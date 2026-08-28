"""samples.py：样卡生成与配置指纹（structure-contract 3.5/3.13/4.1；V2.5 样卡持久化于任务）。

V2.5：样卡经 POST /tasks/{task_id}/samples 落 SAMPLE_GENERATING，执行器样卡 worker
真实生成并持久化（见 services/tasks/executor.py）。本模块提供：
- ``sample_cards_llm``：按配置逐启用难度调用真实生成器（V5A adapter——generator prompt +
  generator-output schema 双消息信封，§5.7），Schema 校验后产出轻量样卡（3.13）；
- ``config_fingerprint``：配置确定性摘要（4.1，start 校验 sample_config_hash 用）。

实现口径：
- 输入为任务快照（首章名 + 页区间）→ load_pages 取章节文本，按
  settings.generator_max_input_chars 累计截断（样卡预览轻量上下文）；
- 每个启用难度一次 chat（比例 > 0 的难度各 1 张，契约 3.5；V2.5 三档均 QUESTION
  卡型，契约 3.6 组合规则——判断题属前两档，后续任务按需引入）；
- 密钥由执行器解密并以带 Key 的 client 注入（红线 4：明文仅存在于 infra/llm 实例）；
- 输出先做强信封校验（parse_cards_json：非 JSON / 无 cards → GENERATION_FAILED），
  再逐卡 Card v1 校验（Schema 是唯一入库门槛，§5.6），非法输出不降格不入库；
- 样卡调用暂不写 llm_call_attempts 账本（账本 stage 枚举仅正式流水线阶段；样卡失败
  经任务 FAILED + error_code 可观测，重试走任务重试链路）。
"""

import hashlib
import json
import logging
import uuid
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig
from infra.db.models import Task, TextChunk
from infra.llm.deepseek import DeepSeekClient
from infra.llm.prompts import load_asset, safe_json_dumps
from services.generation.llm_metrics import observe_llm_call
from services.generation.response_parse import parse_cards_json, to_internal_card
from services.generation.schema_validator import load_card_schema, validate_card
from services.generation.validate import validate_config
from services.pdf.text_chunks import load_pages

logger = logging.getLogger(__name__)

# 启用难度顺序（1 基础 + 1 理解 + 1 深问的构成语义；比例为 0 的难度不生成）
_ENABLED_DIFFICULTIES: tuple[str, ...] = ("BASIC", "UNDERSTANDING", "DEEP_QUESTION")


def sample_cards_llm(
    session: Session,
    *,
    task: Task,
    config: GenerationConfig,
    client: DeepSeekClient,
    settings: Settings,
) -> list[dict[str, object]]:
    """按配置真实生成 1~3 张样卡（启用难度各 1 张；纯生成不入库）。

    调用方（执行器样卡 worker）负责：任务快照完整性前置校验、密钥解密（client 注入）、
    失败兜底（AppError → 任务 FAILED + error_code；输入类 ValueError → GENERATION_FAILED）。
    """
    validate_config(config)
    snapshot = json.loads(task.selected_chapters)
    if not isinstance(snapshot, list) or not snapshot:
        raise ValueError("章节快照为空")
    first = snapshot[0]
    chapter_name = str(first["name"])
    pages = load_pages(
        session,
        file_id=task.file_id,
        start_page=int(first["start_page"]),
        end_page=int(first["end_page"]),
    )
    if not pages:
        raise ValueError("章节无文本内容")
    visible = _cap_pages(pages, settings.generator_max_input_chars)
    ratio = config.difficulty_ratio
    enabled = [
        difficulty
        for difficulty in _ENABLED_DIFFICULTIES
        if getattr(ratio, difficulty.lower()) > 0
    ]
    card_schema = load_card_schema()
    cards: list[dict[str, object]] = []
    for difficulty in enabled:
        system_prompt, user_prompt = _build_prompts(
            config, chapter_name=chapter_name, difficulty=difficulty, pages=visible
        )
        # 模型输出形状偶发漂移（json_object 模式不保证键名）：非法输出重试 1 次
        # （与正式批量链账本内重试同语义；两次均非法 → GENERATION_FAILED）。
        for attempt in range(2):
            result = client.chat(
                user_prompt,
                system_prompt=system_prompt,
                max_tokens=settings.generator_max_output_tokens,
            )
            observe_llm_call(result)
            raw_cards = parse_cards_json(str(result["content"]))
            if not raw_cards:
                _log_invalid_output(difficulty, attempt, result)
                continue
            internal = to_internal_card(raw_cards[0])
            if validate_card(internal, card_schema):
                _log_invalid_output(difficulty, attempt, result)
                continue
            cards.append(_to_sample_card(internal, difficulty=difficulty))
            break
        else:
            raise AppError(ErrorCode.GENERATION_FAILED, "样卡生成输出无效")
    return cards


def _log_invalid_output(difficulty: str, attempt: int, result: dict[str, Any]) -> None:
    """非法输出诊断日志：仅截断的模型内容片段（不含 Key/PDF/Prompt，红线 4/AC-08）。"""
    snippet = str(result.get("content", ""))[:200]
    logger.warning(
        "sample generation invalid output",
        extra={"difficulty": difficulty, "attempt": attempt, "output_snippet": snippet},
    )


def _cap_pages(pages: Sequence[TextChunk], max_chars: int) -> list[dict[str, object]]:
    """页文本按累计字符数截断（与规划/生成链同口径：不裁剪行尾外的内容即可）。"""
    visible: list[dict[str, object]] = []
    used = 0
    for page in pages:
        if used >= max_chars:
            break
        content = page.content or ""
        remaining = max_chars - used
        visible.append(
            {"page_number": page.page_number, "content": content[:remaining]}
        )
        used += min(len(content), remaining)
    return visible


def _build_prompts(
    config: GenerationConfig,
    *,
    chapter_name: str,
    difficulty: str,
    pages: list[dict[str, object]],
) -> tuple[str, str]:
    """Generator 双消息组装（spec §5.7 Generator 行，与正式批次同款）。

    稳定 system（generator prompt v3 + generator-output schema v2 原文）+ 动态 user
    （<GENERATOR_INPUT> 安全 JSON 信封，safe_json_dumps 确定性序列化 + 信封边界转义）。
    样卡 learning_objective 取章节名（样卡为预览性质——真实知识点由规划阶段产出）。
    """
    system_prompt = (
        f"{load_asset('prompts', 'generator')}\n\n<GENERATOR_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'generator_output')}\n</GENERATOR_OUTPUT_SCHEMA>"
    )
    payload = {
        "learning_objective": chapter_name,
        "target_difficulty": difficulty,
        "card_type": "QUESTION",  # V2.5 契约 3.6：三档均 QUESTION（判断题另行引入）
        "source_material": pages,
        "custom_requirements": config.custom_requirements,
    }
    return system_prompt, f"<GENERATOR_INPUT>{safe_json_dumps(payload)}</GENERATOR_INPUT>"


def _to_sample_card(internal: dict[str, object], *, difficulty: str) -> dict[str, object]:
    """响应卡（Card v1 校验后）→ 轻量样卡组件（3.13：无落库/归属/版本字段）。"""
    ctype = str(internal.get("type") or "QUESTION")
    is_tf = ctype == "TRUE_FALSE"
    return {
        "card_id": str(uuid.uuid4()),
        "source": "GENERATED",
        "front": internal.get("front"),
        "back": internal.get("back"),
        "card_type": ctype,
        "statement": internal.get("statement") if is_tf else None,
        "answer_boolean": internal.get("answer_boolean") if is_tf else None,
        "explanation": internal.get("explanation") if is_tf else None,
        "generation_item_id": str(uuid.uuid4()),
        "target_difficulty": difficulty,
        "version": "v1",
    }


def config_fingerprint(config: GenerationConfig | dict[str, object]) -> str:
    """sample_config_hash 配置指纹（4.1）：确定性摘要——同配置同值，配置变则变。

    输入为 GenerationConfig（任务创建/更新路径）或 model_dump dict（worker 读取
    已持久化 JSON 后构造/重放）；规范化序列化（sort_keys + 紧凑分隔符）保证
    dict 与模型两条路径同值。
    """
    data = config.model_dump() if isinstance(config, GenerationConfig) else config
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

"""samples.py：样卡生成与配置指纹（structure-contract 3.5/3.13/4.1；V2.5 样卡持久化于任务）。

V2.5：旧 POST /samples 兼容路径随 Task 5 移除——样卡经
POST /tasks/{task_id}/samples 落 SAMPLE_GENERATING，执行器样卡 worker 生成并
持久化（见 services/tasks/executor.py）。本模块提供纯生成（不入库）与
sample_config_hash 指纹：
- `sample_cards`：按配置生成 1~3 张样卡——比例为 0 的难度不生成（契约 3.5
  「比例为 0 的难度不生成单元和样卡」），启用难度各 1 张；样卡为轻量组件
  （3.13，不含落库/归属/版本字段）。
- `config_fingerprint`：配置确定性摘要（覆盖模式 + 难度比例 + 自定义要求），
  start 校验 sample_config_hash 用（4.1）。
"""

import hashlib
import json

from app.schemas.samples import GenerationConfig
from services.generation.fake import generate_card
from services.generation.validate import validate_config

# 难度 → 样卡主题标签（启用难度各 1 张；1 基础 + 1 理解 + 1 深问的构成语义）
_DIFFICULTY_LABEL: list[tuple[str, str]] = [
    ("BASIC", "基础"),
    ("UNDERSTANDING", "理解"),
    ("DEEP_QUESTION", "深问"),
]


def sample_cards(
    config: GenerationConfig, *, chapter_name: str, task_id: str
) -> list[dict[str, object]]:
    """按配置生成 1~3 张样卡（比例为 0 的难度不生成）。

    纯函数（不入库）：章节名取任务快照（章节删除后名称可还原），task_id 纳入
    fake seed（F-1：同任务同章节不互相去重）。
    """
    validate_config(config)
    ratio = config.difficulty_ratio
    enabled = {
        "BASIC": ratio.basic,
        "UNDERSTANDING": ratio.understanding,
        "DEEP_QUESTION": ratio.deep_question,
    }
    return [
        generate_card(
            f"样卡主题-{label}",
            chapter_name,
            difficulty,
            config.custom_requirements,
            task_id,
        )
        for difficulty, label in _DIFFICULTY_LABEL
        if enabled[difficulty] > 0
    ]


def config_fingerprint(config: GenerationConfig | dict[str, object]) -> str:
    """sample_config_hash 配置指纹（4.1）：确定性摘要——同配置同值，配置变则变。

    输入为 GenerationConfig（任务创建/更新路径）或 model_dump dict（worker 读取
    已持久化 JSON 后构造/重放）；规范化序列化（sort_keys + 紧凑分隔符）保证
    dict 与模型两条路径同值。
    """
    data = config.model_dump() if isinstance(config, GenerationConfig) else config
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

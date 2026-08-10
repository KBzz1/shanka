# V4 样卡、任务与知识点规划 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**结果**：<主 Agent 整包验收通过后在此注明 V4 DONE 与证据位置>

**Goal:** 实现按 manifest 加载/校验 agent 资产、Prompt 稳定前缀与动态后缀分离、固定构成 3 张样卡（不入库）、任务创建/查询/取消/resume（持久化配置与章节、DB 条件更新抢占执行、状态机 PENDING→RUNNING→终态）、KnowledgePoint 规划（COMPACT ≤ BALANCED ≤ EXTENSIVE 可测口径）、任务执行用 deterministic fake 生成器（V5A 换真实分批），使 V4 依据真实验收证据标记 DONE 且 AC-03 通过。

**Architecture:** 契约驱动分层。V4 建立在 F0-F3 地基上：`infra/llm/deepseek.py`（V3B adapter——V4 任务执行用 fake，adapter 由 V5A 接线）、`infra/llm/crypto.py`、`app/middleware/idempotency.py`、`services/pdf`（章节）、`services/decks`/`services/cards`（牌组/卡片）。新增：`infra/llm/prompts.py`（manifest 加载/校验 + 稳定前缀/动态后缀组装）、`services/generation/fake.py`（deterministic fake 生成器：同输入同输出，样卡与任务卡共用）、`services/generation/samples.py`（样卡构成：3 张、1 基础+1 理解+1 应用、2 问答+1 判断、不入库）、`services/generation/planning.py`（KnowledgePoint 规划：章节→分块→知识点，COMPACT≤BALANCED≤EXTENSIVE）、`services/tasks/service.py`（创建/查询/取消/resume、状态机、DB 条件更新抢占）、`services/tasks/executor.py`（进程内 DB 驱动后台循环：RUNNING 任务 fake 执行）、`app/api/samples.py` + `app/api/tasks.py`（handler）、`app/schemas/samples.py` + `app/schemas/tasks.py`。任务执行走 V3A 扫描器同款后台线程模式（4.4 定式）。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0、pytest、ruff、mypy strict。

## Global Constraints

- 所有命令经 `conda run -n shanka-backend ...` 执行（在 `main/` 下），禁止向 base/系统 Python 装依赖。
- 契约权威：structure-contract 3.4/3.5/3.6/4.1/4.2/6.3/6.4/6.9、database-design 2.5/2.6/2.7/§3、PRD 5.4/5.6/AC-03、openapi /samples 与 /tasks 端点及 SampleRequest/TaskCreateRequest/Task/KnowledgePoint schema。实现不得修改 `docs/PRD/`、`docs/Architecture/`。
- **agent 资产**（Architecture AGENTS.md 6/红线 5）：按 `agent_evolution/manifest.json` 加载（prompts.planner/generator、schemas.card、rubrics.main）；资产演进=新版本目录+manifest+CHANGELOG（R-03：不原地改 v1）；加载时校验版本/路径（F0 manifest 守卫已覆盖格式，V4 实现运行时加载）。
- **Prompt 组装**：稳定前缀（系统指令，随 manifest 加载）+ 动态后缀（章节文本/知识点/配置）；完整 Prompt 不落日志/响应（红线 4/AC-08）。
- **样卡**（6.3/AC-03）：POST /samples 豁免幂等键（契约 1.3）；固定 3 张（1 基础+1 理解+1 应用；2 问答+1 判断）；不入库不参与统计；请求体 SampleRequest（file_id/chapter_ids/generation_config 必填）。
- **任务**（6.4/4.1/4.2）：POST /tasks（幂等，TaskCreateRequest 四必填）；创建时校验 file/deck/chapters 归属（404）、generation_config（非法比例 → 400 VALIDATION_ERROR——**裁决**：difficulty_ratio 三值之和=1 且各 ∈ (0,1)；quantity_tendency 枚举）、无已保存 Key → 422 API_KEY_NOT_SET（6.2）；状态机 PENDING→RUNNING→COMPLETED/FAILED/CANCELLED（V5B 补 PAUSED/心跳/孤儿恢复）；`resume`/`cancel` DB 条件更新抢占（4.1：`PAUSED AND resumable=1` 原子转移——V4 的 PAUSED 仅由 resume/cancel 语义保留，无心跳）。
- **KnowledgePoint 规划**（5.4.1/3.6）：每选定章节 → 分块（source_chunk_id）→ 知识点（topic/priority/status=PENDING）；**COMPACT 规划的知识点数 ≤ BALANCED ≤ EXTENSIVE（同章节同输入可测）**——实现：每章知识点数 = 章节文本分块数 × 密度系数（COMPACT=1、BALANCED=2、EXTENSIVE=3，确定性）。
- **任务执行（V4 fake）**：deterministic fake 生成器（services/generation/fake.py：同输入同输出——按知识点构造卡 front/back，难度循环）；后台线程循环（V3A 模式）扫描 RUNNING 任务 → 逐知识点 fake 生成 → 入库（cards + review_states 初始，V1 模式）→ 任务 COMPLETED；V5A 换真实分批生成 + Schema 校验（fake 不代替生产 adapter——红线）。fake 卡含 generation_item_id（确定性 UUID 派生）→ 不重复入库（部分唯一索引）。
- **样卡/任务卡共用 fake 生成器**；样卡不落库（仅响应）。
- **R-11 收口**：任务创建走用户指定 deck_id（TaskCreateRequest），本期无 GENERATED 牌组创建路径；R-11 登记 RESOLVED（契约 3.8 枚举保留，database-design 2.8 派生遗漏说明已核对，V4 不新建 GENERATED 来源）。
- 时间格式唯一规范（database-design §0）；`format_utc`；错误响应 1.4 形状；跨设备统一 404。
- 工作包边界：V4 不含真实 DeepSeek 调用（V5A）、Rubric（V5A）、checkpoint/心跳/孤儿恢复（V5B）、单卡重写（V6）；`app/api/` 其他占位模块不得改动。
- ruff line-length 100、mypy strict；四工具命令全绿；测试命名 `test_<模块>_<行为>`；提交只 `git add` 本任务文件。
- Task 1~5 由实现 subagent 完成；Task 6/7 仅主 Agent 执行（验收 + Progress 更新），subagent 不得触碰 Progress。

---

### Task 1: manifest 加载 + Prompt 组装（infra/llm/prompts）

**Files:**
- Create: `main/infra/llm/prompts.py`
- Create: `main/tests/unit/test_prompts.py`

**Interfaces:**
- Consumes: agent_evolution/manifest.json（只读）、F0 errors
- Produces: `infra.llm.prompts.load_manifest() -> dict`（读取+基础校验，失败抛 AppError(INTERNAL_ERROR)）；`infra.llm.prompts.load_asset(section, name) -> str`（按 manifest 路径读取文本）；`infra.llm.prompts.asset_versions() -> dict`（prompt/schema/rubric 版本——V5A 观测用）；`infra.llm.prompts.build_generation_prompt(prompt_asset: str, *, topic: str, chapter_name: str, difficulty: str, custom_requirements: str | None, card_schema: str) -> str`（稳定前缀（资产）+ 动态后缀（topic/chapter/difficulty/custom/JSON schema 提示）——返回完整 prompt；不落日志）；Task 2 样卡与 Task 3 规划消费

- [ ] **Step 1: 写失败单元测试 `main/tests/unit/test_prompts.py`**

```python
"""infra.llm.prompts 单元测试：manifest 加载/Prompt 组装。"""

import pytest

from app.errors import AppError, ErrorCode
from infra.llm.prompts import (
    asset_versions,
    build_generation_prompt,
    load_asset,
    load_manifest,
)


def test_prompts_load_manifest_valid() -> None:
    manifest = load_manifest()
    assert "prompts" in manifest and "schemas" in manifest and "rubrics" in manifest


def test_prompts_load_asset_exists() -> None:
    generator = load_asset("prompts", "generator")
    assert "generator" in generator or len(generator) > 50


def test_prompts_asset_versions_match_manifest() -> None:
    versions = asset_versions()
    assert versions["prompt_version"] == "v1"
    assert versions["schema_version"] == "v1"
    assert versions["rubric_version"] == "v1"


def test_prompts_build_generation_prompt_stable_and_dynamic() -> None:
    prefix = "你是闪卡生成助手。请根据以下内容生成卡片。"
    prompt = build_generation_prompt(
        prefix,
        topic="FSRS 间隔重复",
        chapter_name="第一章",
        difficulty="BASIC",
        custom_requirements="使用中文",
        card_schema='{"front": "string"}',
    )
    assert prompt.startswith(prefix)  # 稳定前缀保留
    assert "FSRS 间隔重复" in prompt  # 动态后缀
    assert "第一章" in prompt
    assert "BASIC" in prompt
    assert "使用中文" in prompt


def test_prompts_build_without_custom() -> None:
    prefix = "PREFIX"
    prompt = build_generation_prompt(prefix, topic="t", chapter_name="c", difficulty="BASIC", custom_requirements=None, card_schema="{}")
    assert "PREFIX" in prompt and "t" in prompt and "custom" not in prompt.lower()
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_prompts.py -v`
Expected: FAIL（ModuleNotFoundError: infra.llm.prompts）

- [ ] **Step 3: 实现 `main/infra/llm/prompts.py`**

```python
"""agent 资产加载与 Prompt 组装（Architecture AGENTS.md 6/红线 5）。

- manifest.json 为唯一版本入口：prompts.planner/generator、schemas.card、rubrics.main；
- 资产路径相对 agent_evolution/；加载时校验存在；
- Prompt = 稳定前缀（资产，系统指令）+ 动态后缀（topic/chapter/difficulty/custom/JSON schema）；
- 完整 Prompt 不落日志（红线 4/AC-08）。
"""

import json
from pathlib import Path

from app.errors import AppError, ErrorCode

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MANIFEST_PATH = _REPO_ROOT / "agent_evolution" / "manifest.json"
_ASSETS_ROOT = _REPO_ROOT / "agent_evolution"


def load_manifest() -> dict:
    try:
        with _MANIFEST_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise AppError(ErrorCode.INTERNAL_ERROR, "agent 资产 manifest 加载失败") from exc


def load_asset(section: str, name: str) -> str:
    manifest = load_manifest()
    try:
        entry = manifest[section][name]
        path = _ASSETS_ROOT / entry["path"]
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        raise AppError(ErrorCode.INTERNAL_ERROR, f"agent 资产加载失败: {section}/{name}") from exc


def asset_versions() -> dict[str, str]:
    manifest = load_manifest()
    return {
        "prompt_version": manifest["prompts"]["generator"]["version"],
        "schema_version": manifest["schemas"]["card"]["version"],
        "rubric_version": manifest["rubrics"]["main"]["version"],
    }


def build_generation_prompt(
    prompt_asset: str, *, topic: str, chapter_name: str, difficulty: str,
    custom_requirements: str | None, card_schema: str,
) -> str:
    """稳定前缀（资产）+ 动态后缀。返回完整 prompt（调用方保证不落日志）。"""
    parts = [prompt_asset.strip(), f"主题：{topic}", f"章节：{chapter_name}", f"难度：{difficulty}"]
    if custom_requirements:
        parts.append(f"自定义要求：{custom_requirements}")
    parts.append(f"请严格按以下 JSON Schema 输出：\n{card_schema}")
    return "\n".join(parts)
```

- [ ] **Step 4: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_prompts.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy infra/llm/prompts.py tests/unit/test_prompts.py`
Expected: PASS（`_REPO_ROOT` parents 层级核对：main/infra/llm/prompts.py → parents[4] = 仓库根）

- [ ] **Step 5: 提交**

```bash
git add main/infra/llm/prompts.py main/tests/unit/test_prompts.py
git commit -m "feat(prompts): manifest 加载 + Prompt 稳定前缀/动态后缀组装"
```

---

### Task 2: GenerationConfig 校验 + fake 生成器 + 样卡 service

**Files:**
- Create: `main/services/generation/fake.py`
- Create: `main/services/generation/samples.py`
- Create: `main/tests/unit/test_generation_fake.py`
- Create: `main/tests/integration/test_generation_samples.py`

**Interfaces:**
- Consumes: F1 models、V1 cards service（review_state 初始化模式）、prompts（build_generation_prompt）
- Produces: `services.generation.fake.generate_card(topic, chapter_name, difficulty, custom_requirements) -> dict`（deterministic：同输入同输出——用 hashlib 派生 card_id/generation_item_id；构造 Card 视图（card_type 按难度/序号轮换？——**决策**：fake 生成器按 difficulty 生成固定结构：BASIC→QUESTION（front=topic 定义问）、UNDERSTANDING→QUESTION、APPLICATION→TRUE_FALSE——样卡构成 2 问答+1 判断天然满足）；`services.generation.samples.generate_samples(session, *, device_id, file_id, chapter_ids, config) -> list[dict]`（校验 file/chapters 归属 → 3 张样卡（1 基础+1 理解+1 应用；2 问答+1 判断）→ 不入库）；`services.generation.validate_config(config) -> None`（difficulty_ratio 和=1 且各>0；quantity_tendency 枚举 → VALIDATION_ERROR 400）；Task 3 任务执行与 Task 4 handler 消费

- [ ] **Step 1: 写失败单元测试 `main/tests/unit/test_generation_fake.py`**

```python
"""services.generation.fake 确定性单元测试。"""

from services.generation.fake import generate_card


def test_fake_card_deterministic() -> None:
    c1 = generate_card("FSRS", "第一章", "BASIC", None)
    c2 = generate_card("FSRS", "第一章", "BASIC", None)
    assert c1 == c2  # 同输入同输出
    assert c1["front"] and c1["back"]


def test_fake_card_differs_by_input() -> None:
    a = generate_card("主题A", "第一章", "BASIC", None)
    b = generate_card("主题B", "第一章", "BASIC", None)
    assert a["front"] != b["front"]


def test_fake_card_type_by_difficulty() -> None:
    basic = generate_card("t", "c", "BASIC", None)
    app = generate_card("t", "c", "APPLICATION", None)
    assert basic["card_type"] == "QUESTION"
    assert app["card_type"] == "TRUE_FALSE"


def test_fake_card_ids_stable_and_unique() -> None:
    a = generate_card("主题X", "c", "BASIC", None)
    b = generate_card("主题X", "c", "APPLICATION", None)
    assert a["card_id"] != b["card_id"]
    assert a["generation_item_id"] != b["generation_item_id"]
```

- [ ] **Step 2: 写失败集成测试 `main/tests/integration/test_generation_samples.py`**

```python
"""样卡 service 集成测试：构成/不入库/校验（真实 SQLite）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Card, Chapter, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from services.generation.samples import generate_samples
from services.generation.validate import validate_config


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'samples.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_pdf(session: Session, *, device_id: str) -> tuple[str, list[str]]:
    pdf = PdfFile(file_id=_uuid(), device_id=device_id, filename="b.pdf", storage_key=_uuid(),
                  size_bytes=10, status="PARSED", created_at="2026-08-11T00:00:00.000Z")
    session.add(pdf)
    session.flush()
    chapter_ids = []
    for i in range(2):
        ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name=f"第{i+1}章", start_page=i + 1, end_page=i + 2)
        session.add(ch)
        session.flush()
        chapter_ids.append(ch.chapter_id)
    return pdf.file_id, chapter_ids


def _config(quantity: str = "BALANCED") -> dict:
    return {"quantity_tendency": quantity, "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2}}


def test_samples_generates_three_not_persisted(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        file_id, chapter_ids = _seed_pdf(session, device_id=device)
        session.commit()
    with session_factory() as session:
        cards = generate_samples(session, device_id=device, file_id=file_id, chapter_ids=chapter_ids, config=_config())
        session.commit()
    assert len(cards) == 3
    assert {c["target_difficulty"] for c in cards} == {"BASIC", "UNDERSTANDING", "APPLICATION"}
    q_count = sum(1 for c in cards if c["card_type"] == "QUESTION")
    tf_count = sum(1 for c in cards if c["card_type"] == "TRUE_FALSE")
    assert q_count == 2 and tf_count == 1
    with session_factory() as session:
        assert session.scalar(select(Card).limit(1)) is None  # 不入库


def test_samples_cross_device_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        file_id, chapter_ids = _seed_pdf(session, device_id=device)
        session.commit()
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            generate_samples(session, device_id=_uuid(), file_id=file_id, chapter_ids=chapter_ids, config=_config())
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_samples_chapter_not_in_file_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        file_id, chapter_ids = _seed_pdf(session, device_id=device)
        session.commit()
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            generate_samples(session, device_id=device, file_id=file_id, chapter_ids=[_uuid()], config=_config())
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_samples_validate_config() -> None:
    validate_config(_config())  # 合法
    with pytest.raises(AppError) as excinfo:
        validate_config({"quantity_tendency": "BALANCED", "difficulty_ratio": {"basic": 0.5, "understanding": 0.5, "application": 0.2}})
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    with pytest.raises(AppError) as excinfo:
        validate_config({"quantity_tendency": "HUGE", "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2}})
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
```

- [ ] **Step 3: 实现 fake.py + samples.py + validate.py**

```python
"""fake.py：deterministic fake 生成器（V4 任务执行用；V5A 换真实 adapter，红线：fake 不代替生产）。"""

import hashlib
import uuid

_DIFFICULTY_LABEL = {"BASIC": "基础记忆", "UNDERSTANDING": "理解分析", "APPLICATION": "综合应用"}


def _stable_uuid(seed: str) -> str:
    return str(uuid.UUID(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]))


def generate_card(topic: str, chapter_name: str, difficulty: str, custom_requirements: str | None) -> dict:
    seed = f"{topic}|{chapter_name}|{difficulty}|{custom_requirements or ''}"
    card_id = _stable_uuid(f"card|{seed}")
    gen_item = _stable_uuid(f"gen|{seed}")
    label = _DIFFICULTY_LABEL.get(difficulty, difficulty)
    is_tf = difficulty == "APPLICATION"
    front = f"【{label}】{topic}（来自《{chapter_name}》）"
    back = f"参考答案：{topic} 的核心要点（{label} 口径）"
    return {
        "card_id": card_id,
        "source": "GENERATED",
        "front": front,
        "back": back,
        "card_type": "TRUE_FALSE" if is_tf else "QUESTION",
        "statement": front if is_tf else None,
        "answer_boolean": True if is_tf else None,
        "explanation": back if is_tf else None,
        "generation_item_id": gen_item,
        "target_difficulty": difficulty,
        "version": "v1",
    }


"""validate.py：GenerationConfig 校验（3.5）。"""

from app.errors import AppError, ErrorCode

_VALID_TENDENCY = {"COMPACT", "BALANCED", "EXTENSIVE"}


def validate_config(config: dict) -> None:
    tendency = config.get("quantity_tendency")
    if tendency not in _VALID_TENDENCY:
        raise AppError(ErrorCode.VALIDATION_ERROR, "非法 quantity_tendency")
    ratio = config.get("difficulty_ratio") or {}
    try:
        total = sum(float(ratio.get(k, 0)) for k in ("basic", "understanding", "application"))
        ok = all(float(ratio.get(k, 0)) > 0 for k in ("basic", "understanding", "application"))
    except (TypeError, ValueError):
        ok = False
        total = 0.0
    if not ok or abs(total - 1.0) > 1e-9:
        raise AppError(ErrorCode.VALIDATION_ERROR, "difficulty_ratio 必须三值>0 且和为 1")


"""samples.py：样卡 service（6.3/AC-03）。"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Chapter, PdfFile
from services.generation.fake import generate_card
from services.generation.validate import validate_config


def _owned_pdf(session: Session, *, device_id: str, file_id: str) -> PdfFile:
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.device_id != device_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    return pdf


def generate_samples(session: Session, *, device_id: str, file_id: str, chapter_ids: list[str], config: dict) -> list[dict]:
    validate_config(config)
    pdf = _owned_pdf(session, device_id=device_id, file_id=file_id)
    chapters = session.scalars(select(Chapter).where(Chapter.file_id == pdf.file_id)).all()
    by_id = {ch.chapter_id: ch for ch in chapters}
    missing = [cid for cid in chapter_ids if cid not in by_id]
    if missing:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "章节不属于该 PDF")
    # 样卡：1 基础 + 1 理解 + 1 应用；2 问答 + 1 判断（fake 生成器按难度定类型）
    first = by_id[chapter_ids[0]]
    return [
        generate_card("样卡主题-基础", first.name, "BASIC", config.get("custom_requirements")),
        generate_card("样卡主题-理解", first.name, "UNDERSTANDING", config.get("custom_requirements")),
        generate_card("样卡主题-应用", first.name, "APPLICATION", config.get("custom_requirements")),
    ]
```

- [ ] **Step 4: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_generation_fake.py tests/integration/test_generation_samples.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy services/generation/ tests/unit/test_generation_fake.py tests/integration/test_generation_samples.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add main/services/generation/fake.py main/services/generation/samples.py main/services/generation/validate.py main/tests/unit/test_generation_fake.py main/tests/integration/test_generation_samples.py
git commit -m "feat(generation): fake 生成器 + GenerationConfig 校验 + 样卡构成（不入库）"
```

---

### Task 3: 任务 service（创建/规划/状态机/查询/取消/resume）

**Files:**
- Create: `main/services/generation/planning.py`
- Create: `main/services/tasks/service.py`
- Create: `main/tests/integration/test_planning.py`
- Create: `main/tests/integration/test_tasks_service.py`

**Interfaces:**
- Consumes: Task 2 validate/fake、V1/V3A service 模式、F1 models（Task/KnowledgePoint/Batch）
- Produces: `services.generation.planning.plan_knowledge_points(session, *, task_id, chapter_ids, quantity_tendency) -> list[KnowledgePoint]`（每章分块（章节文本——**决策**：V4 无真实章节文本（PDF 解析只存章节元数据）——规划用确定性分块：每章生成 chunk_count 个知识点，chunk_count = 基础 3 × 密度系数（COMPACT=1/BALANCED=2/EXTENSIVE=3）——topic 用"第X章-知识点N"确定性命名；**R-13 注释**：真实分块（文本抽取）在 V5A 或后续接入，V4 规划结构正确）；`services.tasks.service.create_task(session, *, device_id, file_id, deck_id, chapter_ids, config, now) -> Task`（校验归属/配置/已保存 Key（无 → API_KEY_NOT_SET 422）→ 建 Task（PENDING + selected_chapters/generation_config JSON 快照）→ 规划知识点 → 置 RUNNING + stage=PLANNING→GENERATING → 返回 Task 视图）；`services.tasks.service.get_task(session, *, device_id, task_id) -> Task`（404）、`cancel_task`（RUNNING/PENDING → CANCELLED；条件更新）、`resume_task`（PAUSED AND resumable=1 → RUNNING；否则 409 TASK_STATE_CONFLICT）；`services.tasks.service.task_view(task) -> dict`；Task 4 executor 与 handler 消费

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_planning.py`**

```python
"""KnowledgePoint 规划集成测试（3.5/5.4.1 可测口径）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from infra.db.models import Base, Chapter, KnowledgePoint, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from services.generation.planning import plan_knowledge_points


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'plan.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_chapters(session: Session, *, file_id: str, n: int = 2) -> list[str]:
    ids = []
    for i in range(n):
        ch = Chapter(chapter_id=_uuid(), file_id=file_id, name=f"第{i+1}章", start_page=i + 1, end_page=i + 2)
        session.add(ch)
        session.flush()
        ids.append(ch.chapter_id)
    return ids


def test_planning_compact_le_balanced_le_extensive(session_factory: Callable[[], Session]) -> None:
    """同章节同输入：COMPACT 知识点数 ≤ BALANCED ≤ EXTENSIVE（5.4.1 可测口径）。"""
    task_id = _uuid()
    with session_factory() as session:
        pdf = PdfFile(file_id=_uuid(), device_id=_uuid(), filename="b.pdf", storage_key=_uuid(), size_bytes=1, status="PARSED", created_at="2026-08-11T00:00:00.000Z")
        session.add(pdf)
        session.flush()
        chapter_ids = _seed_chapters(session, file_id=pdf.file_id, n=2)
        session.commit()
    counts = {}
    for tendency in ("COMPACT", "BALANCED", "EXTENSIVE"):
        with session_factory() as session:
            kps = plan_knowledge_points(session, task_id=task_id, chapter_ids=chapter_ids, quantity_tendency=tendency)
            counts[tendency] = len(kps)
            session.commit()
        assert counts[tendency] > 0
    assert counts["COMPACT"] <= counts["BALANCED"] <= counts["EXTENSIVE"]


def test_planning_knowledge_point_fields(session_factory: Callable[[], Session]) -> None:
    task_id = _uuid()
    with session_factory() as session:
        pdf = PdfFile(file_id=_uuid(), device_id=_uuid(), filename="b.pdf", storage_key=_uuid(), size_bytes=1, status="PARSED", created_at="2026-08-11T00:00:00.000Z")
        session.add(pdf)
        session.flush()
        chapter_ids = _seed_chapters(session, file_id=pdf.file_id, n=1)
        session.commit()
    with session_factory() as session:
        kps = plan_knowledge_points(session, task_id=task_id, chapter_ids=chapter_ids, quantity_tendency="BALANCED")
        session.commit()
    for kp in kps:
        assert kp.task_id == task_id
        assert kp.source_chunk_id
        assert kp.topic
        assert kp.priority >= 1
        assert kp.status == "PENDING"
```

- [ ] **Step 2: 写失败集成测试 `main/tests/integration/test_tasks_service.py`**

```python
"""任务 service 集成测试：创建/状态机/取消/resume/校验（真实 SQLite + fake 执行）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Deck, KnowledgePoint, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.tasks.service import cancel_task, create_task, get_task, resume_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_context(session: Session, *, device_id: str, with_key: bool = True) -> dict:
    from infra.db.models import ApiKey

    pdf = PdfFile(file_id=_uuid(), device_id=device_id, filename="b.pdf", storage_key=_uuid(), size_bytes=1, status="PARSED", created_at="2026-08-11T00:00:00.000Z")
    session.add(pdf)
    session.flush()
    deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
    session.flush()
    chapter_ids = []
    for i in range(2):
        from infra.db.models import Chapter

        ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name=f"第{i+1}章", start_page=i + 1, end_page=i + 2)
        session.add(ch)
        session.flush()
        chapter_ids.append(ch.chapter_id)
    if with_key:
        session.add(ApiKey(device_id=device_id, encrypted_key="enc", status="AVAILABLE", masked_key="sk-****", updated_at="2026-08-11T00:00:00.000Z"))
    session.flush()
    return {"file_id": pdf.file_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _config(tendency: str = "BALANCED") -> dict:
    return {"quantity_tendency": tendency, "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2}}


def test_tasks_create_runs_and_plans(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(session, device_id=device, file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                           chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
        session.commit()
        task_id = task.task_id
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        status = task.status
    assert status == "RUNNING"
    assert len(kps) > 0
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.generation_config  # JSON 快照持久化


def test_tasks_create_without_key_422(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device, with_key=False)
        session.commit()
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            create_task(session, device_id=device, file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                        chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
    assert excinfo.value.code is ErrorCode.API_KEY_NOT_SET


def test_tasks_create_cross_device_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            create_task(session, device_id=_uuid(), file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                        chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_tasks_cancel_keeps_cards(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(session, device_id=device, file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                           chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
        session.commit()
        task_id = task.task_id
        result = cancel_task(session, device_id=device, task_id=task_id, now="2026-08-11T01:00:00.000Z")
        session.commit()
    assert result.status == "CANCELLED"


def test_tasks_resume_paused(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(session, device_id=device, file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                           chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
        session.flush()
        task.status = "PAUSED"
        task.resumable = 1
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        result = resume_task(session, device_id=device, task_id=task_id, now="2026-08-11T02:00:00.000Z")
        session.commit()
    assert result.status == "RUNNING"
    # 再 resume（RUNNING 非 PAUSED）→ 409
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            resume_task(session, device_id=device, task_id=task_id, now="2026-08-11T02:00:00.000Z")
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
```

（说明：`create_task` 的"置 RUNNING + stage"——**决策**：创建即 RUNNING（后台循环立即处理）；stage=PLANNING 在 create_task 内完成规划后置 GENERATING（同步规划，异步生成）。KnowledgePoint 与 Task 同事务。ApiKey 校验：查 api_keys 表 status=AVAILABLE 且可解密？——**决策**：只查 status=AVAILABLE 行存在（不解密——V5A 生成时才解密调用）。）

- [ ] **Step 3: 运行确认失败 → 实现 planning.py + tasks/service.py**

```python
"""planning.py：KnowledgePoint 规划（5.4.1/3.6；COMPACT≤BALANCED≤EXTENSIVE 可测口径）。"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.db.models import Chapter, KnowledgePoint

_DENSITY = {"COMPACT": 1, "BALANCED": 2, "EXTENSIVE": 3}
_BASE_CHUNKS = 3  # 每章基础分块数（确定性；真实文本分块 V5A 接入）


def plan_knowledge_points(session: Session, *, task_id: str, chapter_ids: list[str], quantity_tendency: str) -> list[KnowledgePoint]:
    density = _DENSITY.get(quantity_tendency, 2)
    chapters = session.scalars(select(Chapter).where(Chapter.chapter_id.in_(chapter_ids))).all()
    kps: list[KnowledgePoint] = []
    for ch in chapters:
        for i in range(_BASE_CHUNKS * density):
            kps.append(
                KnowledgePoint(
                    knowledge_point_id=str(uuid.uuid4()),
                    task_id=task_id,
                    chapter_id=ch.chapter_id,
                    source_chunk_id=f"{ch.chapter_id}:chunk{i + 1}",
                    topic=f"{ch.name}-知识点{i + 1}",
                    priority=i + 1,
                    status="PENDING",
                )
            )
    return kps


"""tasks/service.py：任务用例（创建/查询/取消/resume + 状态机 + DB 条件更新）。"""

import json
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, KnowledgePoint, PdfFile, Task
from services.decks.service import _owned as _owned_deck
from services.generation.planning import plan_knowledge_points
from services.generation.validate import validate_config


def _uuid4() -> str:
    return str(uuid.uuid4())


def _owned_task(session: Session, *, device_id: str, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.device_id != device_id:
        raise AppError(ErrorCode.TASK_NOT_FOUND, "任务不存在")
    return task


def _task_view(task: Task) -> dict:
    return {
        "task_id": task.task_id,
        "file_id": task.file_id,
        "deck_id": task.deck_id,
        "status": task.status,
        "stage": task.stage,
        "selected_chapters": json.loads(task.selected_chapters),
        "generation_config": json.loads(task.generation_config),
        "cursor": json.loads(task.cursor) if task.cursor else None,
        "generated_card_count": task.generated_card_count,
        "total_batch_count": task.total_batch_count,
        "completed_batch_count": task.completed_batch_count,
        "resumable": bool(task.resumable),
        "failure_stage": task.failure_stage,
        "error_code": task.error_code,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "updated_at": task.updated_at,
    }


def create_task(session: Session, *, device_id: str, file_id: str, deck_id: str, chapter_ids: list[str], config: dict, now: str) -> Task:
    validate_config(config)
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.device_id != device_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    _owned_deck(session, device_id=device_id, deck_id=deck_id)
    # 已保存 Key 校验（6.2：无 Key → API_KEY_NOT_SET）
    key_row = session.scalar(select(ApiKey).where(ApiKey.device_id == device_id, ApiKey.status == "AVAILABLE"))
    if key_row is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "未保存可用 API Key")
    task = Task(
        task_id=_uuid4(), device_id=device_id, file_id=file_id, deck_id=deck_id,
        status="RUNNING", stage="GENERATING",
        selected_chapters=json.dumps(chapter_ids, ensure_ascii=False),
        generation_config=json.dumps(config, ensure_ascii=False),
        generated_card_count=0, resumable=0,
        created_at=now, started_at=now, updated_at=now,
    )
    session.add(task)
    session.flush()
    plan_knowledge_points(session, task_id=task.task_id, chapter_ids=chapter_ids, quantity_tendency=config["quantity_tendency"])
    return task


def get_task(session: Session, *, device_id: str, task_id: str) -> Task:
    return _owned_task(session, device_id=device_id, task_id=task_id)


def cancel_task(session: Session, *, device_id: str, task_id: str, now: str) -> Task:
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    if task.status in ("PENDING", "RUNNING", "PAUSED"):
        task.status = "CANCELLED"
        task.ended_at = now
        task.updated_at = now
    return task


def resume_task(session: Session, *, device_id: str, task_id: str, now: str) -> Task:
    """DB 条件更新抢占（4.1）：PAUSED AND resumable=1 → RUNNING；否则 409。"""
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    result = session.execute(
        update(Task)
        .where(Task.task_id == task_id, Task.status == "PAUSED", Task.resumable == 1)
        .values(status="RUNNING", updated_at=now)
    )
    if result.rowcount == 0:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务不可恢复")
    session.refresh(task)
    return task
```

- [ ] **Step 4: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_planning.py tests/integration/test_tasks_service.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy services/generation/ services/tasks/ tests/integration/test_planning.py tests/integration/test_tasks_service.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add main/services/generation/planning.py main/services/tasks/service.py main/tests/integration/test_planning.py main/tests/integration/test_tasks_service.py
git commit -m "feat(tasks): 任务创建/状态机/查询/取消/resume + KnowledgePoint 规划（可测口径）"
```

---

### Task 4: 任务执行后台循环（fake）+ api 路由（samples/tasks）

**Files:**
- Create: `main/services/tasks/executor.py`
- Modify: `main/app/config.py`（task_scan_interval_seconds）
- Create: `main/app/schemas/samples.py`、`main/app/schemas/tasks.py`
- Modify: `main/app/api/samples.py`、`main/app/api/tasks.py`（占位重写）
- Modify: `main/app/main.py`（装配 router + 后台循环）
- Create: `main/tests/integration/test_tasks_executor.py`、`main/tests/integration/test_samples_api.py`、`main/tests/integration/test_tasks_api.py`

**Interfaces:**
- Consumes: Task 2/3、V1 cards（入库模式）、F1 幂等
- Produces: `services.tasks.executor.process_running_tasks(session, *, storage=None) -> int`（扫描 RUNNING 任务 → 逐知识点 fake 生成 → 入库（cards + review_state 初始，V1 模式；generation_item_id 部分唯一索引防重）→ generated_card_count 更新 → 全部完成 → COMPLETED；fake 失败（不应发生）→ FAILED）；`executor.scan_once(session_factory) -> int`；路由 `POST /samples`（200 {sample_cards: [3]}；豁免幂等）、`POST /tasks`（201 Task；幂等）、`GET /tasks/{task_id}`、`POST /tasks/{task_id}/resume`、`POST /tasks/{task_id}/cancel`；`app.schemas.samples.SampleRequest`、`app.schemas.tasks.TaskCreateRequest`；main.py 装配 + 后台循环（V3A 模式）

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_tasks_executor.py`**

```python
"""任务执行器集成测试：fake 生成入库/状态机/防重。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.db.models import Base, Card, KnowledgePoint, Task
from infra.db.session import create_db_engine, create_session_factory
from services.tasks.executor import process_running_tasks
from services.tasks.service import create_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'exec.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(session: Session, *, device_id: str) -> str:
    from infra.db.models import ApiKey, Chapter, PdfFile
    from services.decks.service import create_deck

    pdf = PdfFile(file_id=_uuid(), device_id=device_id, filename="b.pdf", storage_key=_uuid(), size_bytes=1, status="PARSED", created_at="2026-08-11T00:00:00.000Z")
    session.add(pdf)
    session.flush()
    deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    session.add(ApiKey(device_id=device_id, encrypted_key="enc", status="AVAILABLE", masked_key="sk-****", updated_at="2026-08-11T00:00:00.000Z"))
    session.flush()
    task = create_task(session, device_id=device_id, file_id=pdf.file_id, deck_id=deck.deck_id,
                       chapter_ids=[ch.chapter_id],
                       config={"quantity_tendency": "COMPACT", "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2}},
                       now="2026-08-11T00:00:00.000Z")
    session.commit()
    return task.task_id


def test_executor_completes_task_and_inserts_cards(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, device_id=device)
    with session_factory() as session:
        n = process_running_tasks(session)
        session.commit()
        task = session.get(Task, task_id)
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert n == 1
    assert task.status == "COMPLETED"
    assert len(cards) == len(kps)  # 每知识点一张卡
    assert task.generated_card_count == len(cards)
    assert all(c.source == "GENERATED" for c in cards)


def test_executor_no_duplicate_generation_items(session_factory: Callable[[], Session]) -> None:
    """generation_item_id 部分唯一索引防重：二次执行不重复入库。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, device_id=device)
    with session_factory() as session:
        process_running_tasks(session)
        session.commit()
    # 已完成任务不再处理
    with session_factory() as session:
        n = process_running_tasks(session)
        session.commit()
    assert n == 0
    with session_factory() as session:
        task = session.get(Task, task_id)
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        item_ids = [c.generation_item_id for c in cards]
    assert len(item_ids) == len(set(item_ids))  # 无重复
```

- [ ] **Step 2: 实现 executor.py + schemas + 路由 + 装配**

```python
"""executor.py：任务执行器（4.4 定式：进程内 DB 驱动；V4 用 deterministic fake，V5A 换真实分批）。"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.db.models import Card, KnowledgePoint, ReviewState, Task
from services.generation.fake import generate_card


def process_running_tasks(session: Session) -> int:
    """处理全部 RUNNING 任务（V4 同步执行：逐知识点 fake 生成入库）。返回处理任务数。"""
    tasks = session.scalars(select(Task).where(Task.status == "RUNNING").order_by(Task.created_at)).all()
    for task in tasks:
        _execute_task(session, task)
    return len(tasks)


def _execute_task(session: Session, task: Task) -> None:
    kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task.task_id).order_by(KnowledgePoint.priority)).all()
    generated = 0
    for kp in kps:
        card = generate_card(kp.topic, kp.chapter_id or "", "BASIC", None)
        # 入库（V1 模式 + generation_item_id 防重）
        existing = session.scalar(select(Card).where(Card.generation_item_id == card["generation_item_id"]))
        if existing is not None:
            continue
        c = Card(
            card_id=card["card_id"], deck_id=task.deck_id, device_id=task.device_id,
            source="GENERATED", position=_next_position(session, deck_id=task.deck_id),
            front=card["front"], back=card["back"], card_type=card["card_type"],
            statement=card.get("statement"), answer_boolean=card.get("answer_boolean"),
            explanation=card.get("explanation"),
            generation_item_id=card["generation_item_id"], target_difficulty=card["target_difficulty"],
            version=card["version"], created_at=task.updated_at, updated_at=task.updated_at,
        )
        session.add(c)
        session.flush()
        session.add(ReviewState(review_state_id=str(uuid.uuid4()), card_id=c.card_id, state="NEW",
                                stability=0.0, difficulty=1.0, due=task.updated_at, reps=0, lapses=0, updated_at=task.updated_at))
        kp.status = "PROCESSED"
        generated += 1
    task.generated_card_count += generated
    task.status = "COMPLETED"
    task.ended_at = task.updated_at
    task.resumable = 0
```

（说明：`_next_position` 复用 V1 逻辑（max+1）；`chapter_id` 的 topic 命名在 fake 中已含章节名——executor 用 kp.topic 直接生成；**difficulty 轮换**：V4 简化用 BASIC（难度分布 V5A 按 ratio 分配——登记）。`scan_once` 与 V3A 同款（session_factory 循环）。**注意**：executor 的卡 difficulty 全部 BASIC——V5A 按 ratio 分配（fake 生成器已支持难度参数，executor 按 kp 序号轮换 BASIC/UNDERSTANDING/APPLICATION 更贴近口径——**决策**：executor 按 kp priority 轮换三档难度）。）

```python
# schemas/samples.py、schemas/tasks.py：SampleRequest/TaskCreateRequest/Task 视图模型（openapi 对齐）
# api/samples.py：POST /samples（无幂等键；file/chapters 校验；返回 {sample_cards: [...]}）
# api/tasks.py：POST /tasks（幂等 execute_idempotent + 校验 + create_task）、GET /tasks/{id}、POST resume/cancel（幂等）
# main.py：include_router(samples.router, tasks.router) + 后台循环（task_scan_interval_seconds=1.0，V3A 模式）
```

- [ ] **Step 3: 写 API 集成测试（samples/tasks）+ 运行确认 + ruff/mypy + 全量**

`test_samples_api.py`：POST /samples（合法 200 + 3 张构成；跨设备 404；非法比例 400；无幂等键豁免——不带 Idempotency-Key 成功）
`test_tasks_api.py`：POST /tasks（201 + RUNNING；无 Key 422；幂等重放；GET 轮询；cancel 200；resume 409/200）
Expected: 全绿

- [ ] **Step 4: 提交**

```bash
git add main/services/tasks/executor.py main/app/config.py main/app/schemas/samples.py main/app/schemas/tasks.py main/app/api/samples.py main/app/api/tasks.py main/app/main.py main/tests/integration/test_tasks_executor.py main/tests/integration/test_samples_api.py main/tests/integration/test_tasks_api.py
git commit -m "feat(tasks-api): 任务执行器（fake 入库/防重）+ samples/tasks 路由"
```

---

### Task 5: acceptance AC-03 + schema 守卫

**Files:**
- Create: `main/tests/contract/test_generation_schemas_guard.py`
- Create: `main/tests/acceptance/test_acceptance_ac03.py`

**Interfaces:**
- Consumes: Task 1-4 全部产物
- Produces: AC-03 验收映射；守卫（Task/KnowledgePoint/SampleRequest/TaskCreateRequest ↔ openapi）

- [ ] **Step 1: 守卫测试 `main/tests/contract/test_generation_schemas_guard.py`**

```python
"""契约守卫：Task/KnowledgePoint ↔ openapi（守卫 1 扩展）。"""

from app.schemas.tasks import Task as TaskView
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_task_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(TaskView, openapi_schema("Task"), load_openapi())
    assert violations == []
```

（说明：Task openapi schema 的 selected_chapters 是 Chapter[]（object 数组）、generation_config 是 $ref GenerationConfig（object）——守卫需支持 object 属性递归（F1 已支持）。若 TaskView 模型字段与实际 openapi 结构有出入（selected_chapters 存 JSON 字符串 vs 数组），**修正**：视图模型 selected_chapters 用 `list[dict] | None`（handler 从 service 视图 dict 直接构造——service task_view 已 json.loads）。以守卫事实为准修正模型。）

- [ ] **Step 2: 验收测试 `main/tests/acceptance/test_acceptance_ac03.py`**

```python
"""验收测试：AC-03 样卡（PRD；迁移 schema + HTTP）。"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac03.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage", rate_limit_ip_per_second=1000)
    return TestClient(create_app(settings))


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _make_pdf_context(client: TestClient, device: dict[str, str]) -> tuple[str, list[str]]:
    """上传+解析样书（复用 V3A 链路）或用种子——**决策**：用最小 PDF 构造（V3A 上传需合法 PDF）——直接种 DB 行（测试内 alembic 后手动插 PdfFile/Chapter）。"""
    ...
```

（说明：AC-03 验收的 PDF 上下文——用种 DB 行（PdfFile PARSED + Chapters）避免样书依赖；或上传样书走全链路。**决策**：种 DB 行（迁移 schema 后 INSERT）——AC-03 聚焦样卡构成。实现者按此。）

- [ ] **Step 3: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/contract/test_generation_schemas_guard.py tests/acceptance/ -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [ ] **Step 4: 提交**

```bash
git add main/tests/contract/test_generation_schemas_guard.py main/tests/acceptance/test_acceptance_ac03.py
git commit -m "test(acceptance): AC-03 样卡验收映射 + Task/KnowledgePoint 守卫"
```

---

### Task 6: 整包验收（仅主 Agent）

**Files:**
- 验证：全部 V4 产物；不新增代码

- [ ] **Step 1: 四工具命令全绿**

Run（均在 `main/`）: `python --version`、`python -m pytest`、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`
Expected: 全绿

- [ ] **Step 2: 干净环境安装 + 迁移**

（V3A/V3B 同款 venv + alembic upgrade 验证）

- [ ] **Step 3: uvicorn 冒烟（样卡 → 任务创建 → 后台执行 → 轮询 COMPLETED → 卡片入库）**

```bash
cd /home/kbzz1/shanka_backend/main
rm -f shanka.db && conda run -n shanka-backend alembic -x database_url="sqlite:///./shanka.db" upgrade head
# 种子：PdfFile/Chapter/Deck/ApiKey（用 python 脚本直连 DB）
conda run -n shanka-backend python - << 'PY'
# 种子脚本（sqlite3 直连或 SQLAlchemy）——实现者/主 Agent 用简单 INSERT
PY
/home/kbzz1/miniconda3/envs/shanka-backend/bin/python -m uvicorn app.main:app --port 8087 > /tmp/v4-uvicorn.log 2>&1 &
sleep 3
DEV=$(conda run -n shanka-backend python -c 'import uuid;print(uuid.uuid4())')
# POST /samples（无幂等键）
curl -s -H "X-Device-ID: $DEV" -X POST -H "Content-Type: application/json" -d '{"file_id":"...","chapter_ids":["..."],"generation_config":{"quantity_tendency":"BALANCED","difficulty_ratio":{"basic":0.4,"understanding":0.4,"application":0.2}}}' http://127.0.0.1:8087/samples
# POST /tasks
KEY=$(conda run -n shanka-backend python -c 'import uuid;print(uuid.uuid4())')
curl -s -H "X-Device-ID: $DEV" -H "Idempotency-Key: $KEY" -X POST -H "Content-Type: application/json" -d '{"file_id":"...","deck_id":"...","chapter_ids":["..."],"generation_config":{...}}' http://127.0.0.1:8087/tasks
sleep 2  # 后台执行
# GET /tasks/{id} → COMPLETED + generated_card_count > 0
kill %1
```
Expected: 样卡 3 张构成正确、任务 COMPLETED、卡片入库（GET /decks/{id}/cards 可见）

- [ ] **Step 4: 关键边界复核（收证据）**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_planning.py tests/integration/test_tasks_service.py tests/integration/test_tasks_executor.py tests/integration/test_samples_api.py tests/integration/test_tasks_api.py tests/acceptance/ tests/contract/ -v`
Expected: 全绿

- [ ] **Step 5: 无明文泄漏抽查**

Run: `grep -rn "sk-" main/app main/services main/infra --include="*.py" || true`
Expected: 无真实泄漏（测试假值除外）

---

### Task 7: 更新 Progress（仅主 Agent）

**Files:**
- Modify: `docs/Progress.md`
- Modify: `docs/superpowers/plans/2026-08-11-v4-samples-tasks-planning.md`（标题下「结果」）

- [ ] **Step 1: 更新 `docs/Progress.md`**

- 第 4 节 V4 行：`TODO` → `DONE`，证据填写：manifest 加载/Prompt 组装、样卡构成（3 张不入库）、GenerationConfig 校验、任务创建/状态机/查询/取消/resume、KnowledgePoint 规划（可测口径）、fake 执行入库防重、AC-03 通过。
- 第 6 节：R-11 → `RESOLVED`（V4 收口：任务创建走用户指定 deck_id，GENERATED 来源未实现，契约枚举保留）。
- 第 1 节状态基线：自动化验证测试数更新。
- 计划文件标题下「结果」注明 V4 DONE 与证据位置。

- [ ] **Step 2: 提交**

```bash
git add docs/Progress.md docs/superpowers/plans/2026-08-11-v4-samples-tasks-planning.md
git commit -m "docs(progress): V4 DONE（样卡、任务与知识点规划），AC-03 通过，R-11 RESOLVED"
```

---

## Self-Review

**1. Spec coverage（对照 Progress V4 文本）：**

| V4 要求 | 落点 |
| --- | --- |
| 按 manifest 加载/校验资产 | Task 1（load_manifest/load_asset/asset_versions） |
| Prompt 稳定前缀与动态后缀分离 | Task 1（build_generation_prompt） |
| 固定构成 3 张样卡且不入库 | Task 2（generate_samples：3 张/难度各 1/2 问答+1 判断/不入库断言） |
| 创建/查询/取消任务 | Task 3（create_task/get_task/cancel_task） |
| 持久化配置/章节 | Task 3（selected_chapters/generation_config JSON 快照） |
| 规划 KnowledgePoint | Task 3（plan_knowledge_points：字段完整 + PENDING） |
| DB 条件更新抢占执行 | Task 3（resume：PAUSED AND resumable=1 原子转移 + 409） |
| 无 Key/章节/牌组、非法比例 | Task 2/3（API_KEY_NOT_SET 422、PDF_NOT_FOUND 404、VALIDATION_ERROR 400） |
| COMPACT ≤ BALANCED ≤ EXTENSIVE | Task 3（planning 测试断言） |
| 自定义要求不继承 | 契约语义：custom_requirements 仅当前任务（V4 快照在 generation_config——继承由客户端携带，V4 不存默认）；Task 1 build_generation_prompt 支持 custom |
| 同 key 单任务 | Task 4（幂等重放测试） |
| 状态转移和 AC-03 | Task 3/5 |
| 任务执行 fake（LOCAL-DONE 红线） | Task 4（deterministic fake + 入库防重 + COMPLETED） |

**2. Placeholder scan：** 全部任务给出完整文件内容与可执行命令。Task 5 的 AC-03 PDF 上下文（种 DB 行 vs 样书）与 Task 6 冒烟种子脚本在"说明"中给出决策；Task 4 executor 的难度轮换在说明中裁决（priority 轮换三档）。无 TBD/TODO 占位。

**3. Type consistency：** `load_manifest() -> dict`/`load_asset(section, name) -> str`/`asset_versions() -> dict`/`build_generation_prompt(...)`（Task 1 定义，Task 2 使用）；`generate_card(topic, chapter_name, difficulty, custom) -> dict`（Task 2 定义，Task 3/4 使用）；`generate_samples(session, *, device_id, file_id, chapter_ids, config) -> list[dict]`（Task 2 定义，Task 4 handler 使用）；`validate_config(config)`（Task 2 定义，Task 3 使用）；`plan_knowledge_points(session, *, task_id, chapter_ids, quantity_tendency) -> list[KnowledgePoint]`（Task 3 定义，create_task 使用）；`create_task/get_task/cancel_task/resume_task`（Task 3 定义，Task 4 handler 使用）；`process_running_tasks(session) -> int`/`scan_once(session_factory) -> int`（Task 4 定义，main.py 装配与 Task 6 验收使用）；`_owned_deck`（V1 定义，Task 3 使用）。

"""R1 本机门槛 1：PRD 7-10 后端可本机验证项路径核对清单（task-1 brief，2026-08-11）。

本文件是核对清单的可执行载体：docstring 表格逐项列 PRD 7.1/7.2/8/9/10 后端可本机验证项 ↔
既有测试名 ↔ 本文件补充用例；只在真有缺口时补用例（已覆盖项写"已由 X 覆盖"，不写冗余用例）。

基线：348 passed 全绿（F0-R1 各包验收过）；本文件新增 2 条 AC-08 内容级缺口用例
（R1 门槛 1 核对发现的唯一缺口：AC-01 原声明"不做内容级断言"）。

PRD 7.1 数据安全（prd_v2_1.md §7.1）：
| 核对项 | 覆盖（既有测试） | 本文件补充 |
| --- | --- | --- |
| Key 服务端加密保存、仅 llm 调用路径使用 | tests/unit/test_crypto.py；tests/integration/test_api_key_service.py（DB 密文无明文）；tests/contract/test_api_key_schemas_guard.py（响应 masked_key） | — |
| Key 不落日志/监控/分析/接口响应 | test_acceptance_ac11_save_and_status（响应无明文）；test_acceptance_ac11_no_plaintext_in_logs（请求日志无明文） | — |
| 资源按数据主体隔离（跨设备 404，不暴露存在性） | test_acceptance_ac09_cards_cross_device_404；test_acceptance_ac06_cross_device_404；test_quality_summary_isolates_by_device（batch/聚合跨设备）；test_decks_service 跨设备 get 404 | — |
| 删除牌组级联删除关联数据 | test_decks_delete_removes_cascade_and_sets_null（cards/review_states 级联 + tasks SET NULL）；test_acceptance_ac09_delete_removes_from_reads（AC-09-3 读取不可见） | — |
| 日志不记录完整 PDF 内容 | 结构性：LoggingMiddleware 只记元数据字段（test_request_logging_emits_json_line 字段集断言 + ac11 body 内容级）；AC-01 上传全流程已声明但未做内容级断言 | test_acceptance_ac08_pdf_upload_content_not_logged |
| 日志不记录完整 Prompt | 代码核查：llm 路径（deepseek.py/executor.py）仅记 type/status/task_id，无日志语句引用 prompt 内容；无既有内容级断言 | test_acceptance_ac08_prompt_content_not_logged |

PRD 7.2 可靠性（§7.2）：
| 核对项 | 覆盖（既有测试） | 本文件补充 |
| --- | --- | --- |
| 分批执行 + 断点续传 | test_acceptance_ac05_crash_resume_cursor_and_dedup（AC-05-a/b：批 2 前崩溃 → 已入库卡保留 + 孤儿 resume 只处理批 2） | — |
| 已完成批次不重复执行 | 同上（AC-05-c：retry_count=0、chat 调用恰 3 次——批 1 未重跑） | — |
| 已入库卡片不重复写入 | 同上（AC-05-d：generation_item_id 互异、批内重复内容只入 1 张）；test_executor_no_duplicate_generation_items；test_acceptance_ac09_import_idempotency_replay | — |
| 失败任务保留已完成结果及可恢复游标 | test_executor_system_failure_fails_task_and_keeps_cards（FAILED + 批 1 卡保留）；test_acceptance_ac05_cancel_keeps_inserted_cards（取消保留）；AC-05 游标原子推进 | — |

PRD 8 核心指标（§8；8.1 目标值为 live 后统计口径，本机只核对观测设施存在）：
| 核对项 | 覆盖（既有测试） | 本文件补充 |
| --- | --- | --- |
| 8.1 观测设施：/metrics 输出 llm_* 指标 | test_metrics_text_includes_llm_generation_batch_metrics（llm_tokens_total kind=cache_hit/miss/output、llm_requests_total、generation_tasks_total、batch_retry_total）；test_metrics_endpoint_returns_prometheus_text | — |
| 8.2 Rubric 各维度均分 + 按模型/PDF/难度分组聚合 | test_quality_summary_aggregates_by_model_pdf_difficulty；test_acceptance_ac07_quality_and_cache_recorded（单卡 5 分数段落库） | — |
| 8.2 覆盖率/重复率 + 难度/章节/卡型分布 | test_acceptance_ac07_quality_and_cache_recorded（coverage_rate/duplicate_rate/三分布列） | — |
| 8.3 Cache 命中/未命中/输出 Token 记录 | test_acceptance_ac07_quality_and_cache_recorded；test_batches_endpoint_lists_usage_versions_quality_and_cost；test_metrics_text_includes_llm_generation_batch_metrics | — |

PRD 9 AC-01~11 验收映射（逐条核对映射存在性，全部已由 V1-V6 acceptance 覆盖）：
| AC | 覆盖（既有测试） | 本文件补充 |
| --- | --- | --- |
| AC-01 | test_acceptance_ac01_sample_book_parses_to_chapters；test_acceptance_ac01_no_toc_stops_flow | — |
| AC-02 | test_acceptance_ac02_chapter_patch | — |
| AC-03 | test_acceptance_ac03_sample_cards（3 张/三档/2 问答+1 判断/不入库） | — |
| AC-04 | test_acceptance_ac04_valid_cards_inserted_and_completed；ac04_invalid_cards_not_inserted_skipped；ac04_rubric_no_auto_fix_prune_or_regenerate | — |
| AC-05 | test_acceptance_ac05_crash_resume_cursor_and_dedup；ac05_cancel_keeps_inserted_cards | — |
| AC-06 | test_acceptance_ac06_rewrite_succeeds；ac06_schema_invalid_preserves_card；ac06_idempotent_replay；ac06_idempotency_conflict；ac06_error_path_no_idempotency_record；ac06_api_key_not_set_422；ac06_corrupted_encrypted_key_502；ac06_cross_device_404 | — |
| AC-07 | test_acceptance_ac07_quality_and_cache_recorded；ac07_abnormal_cache_data_does_not_gate_insertion | — |
| AC-08 | Key 部分：ac11_no_plaintext_in_logs、ac11_save_and_status；PDF/Prompt 内容级此前无断言 | 本文件两条（见 7.1 表） |
| AC-09 | test_acceptance_ac09_deck_card_workflow；ac09_real_progress；ac09_delete_removes_from_reads；ac09_import_empty_front_or_back_422；ac09_cards_cross_device_404；ac09_delete_requires_idempotency_key；ac09_import_idempotency_replay；ac09_concurrent_idempotency_single_side_effect | — |
| AC-10 | test_acceptance_ac10_review_workflow；ac10_dashboard_real_data | — |
| AC-11 | test_acceptance_ac11_save_and_status；ac11_unknown_when_not_saved；ac11_no_plaintext_in_logs | — |

PRD 10 质量策略（§10）：
| 核对项 | 覆盖（既有测试） | 本文件补充 |
| --- | --- | --- |
| Rubric 只观测（不修复/淘汰/补生成） | test_acceptance_ac04_rubric_no_auto_fix_prune_or_regenerate（AC-04-c/d：低分合法卡全入库、retry_count=0、批次数不增加） | — |
| 观测数据异常不改变入库规则 | test_acceptance_ac07_abnormal_cache_data_does_not_gate_insertion（AC-07-c：usage 缺失仍 SUCCEEDED 入库） | — |

生产代码 mock 核对（Step 4）：grep `mock|fake|MagicMock` 于 app/ services/ infra/（排除
tests）——全部命中均为允许项：(1) V4 样卡 fake——services/generation/fake.py，唯一生产消费方
services/generation/samples.py（样卡路径，任务执行已不用，契约设计）；(2) 测试注入点——
client_factory/transport 参数（executor.py、cards.py、rewrite.py、deepseek.py，docstring/
注释说明）；无 MagicMock、无硬编码成功路径。

本文件补充用例（唯一缺口：AC-08 PDF/Prompt 内容级日志断言；生产代码已正确，用例为回归锁定，
非先失败后修复）：
1. test_acceptance_ac08_pdf_upload_content_not_logged —— 真实样书（11MB）HTTP 上传 + 扫描
   全流程，捕获日志断言样书内容片段不出现。
2. test_acceptance_ac08_prompt_content_not_logged —— chat 调用（mock transport 注入，红线 4
   允许项）成功 + 失败两路径，捕获日志断言 Prompt 独有片段不出现。
"""

import logging
import uuid
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.main import create_app
from infra.llm.deepseek import DeepSeekClient
from services.pdf.scanner import scan_once
from tests.conftest import auth_headers

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")

# 样书首页内容片段（pypdf 实测提取）：若日志引用完整 PDF 内容必含此串
_PDF_MARKER = "深入理解 AI Agent"
_PDF_MARKER_2 = "李博杰"

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "r1_paths.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _user(client: TestClient) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _scan(client: TestClient) -> None:
    """显式触发扫描（测试环境无后台循环）：从 app state 取 session_factory/storage。"""
    app = cast(FastAPI, client.app)
    scan_once(app.state.session_factory, storage=app.state.storage)


def test_acceptance_ac08_pdf_upload_content_not_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-08 内容级缺口：上传真实样书（11MB 二进制 body）+ 扫描全流程后，日志无 PDF 内容片段。

    既有覆盖为结构性（LoggingMiddleware 字段集断言 + Key body 内容级断言）；本用例对
    "日志不记录完整 PDF 内容"做内容级锁定（AC-01 原声明"不做内容级断言"，R1 门槛 1 补齐）。
    alembic fileConfig（disable_existing_loggers）在迁移时禁用既有 logger → 临时恢复请求日志
    中间件 logger（ac11 同款模式），使断言真实覆盖请求日志。
    """
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _user(client)
    monkeypatch.setattr(logging.getLogger("app.middleware.logging"), "disabled", False)
    with caplog.at_level(logging.INFO):
        with SAMPLE.open("rb") as f:
            resp = client.post(
                "/pdfs",
                files={"file": ("book.pdf", f, "application/pdf")},
                headers={**device, **_idem()},
            )
        assert resp.status_code == 201
        file_id = resp.json()["file_id"]
        _scan(client)  # 解析路径（提取文本层）也在日志捕获范围内
        body = client.get(f"/pdfs/{file_id}", headers=device).json()
    assert body["status"] == "PARSED"  # 前置：上传 + 解析确实完成（否则断言无意义）
    assert "request complete" in caplog.text  # 请求日志确实产生（LoggingMiddleware 活跃）
    assert _PDF_MARKER not in caplog.text  # 完整 PDF 内容不落日志（7.1 日志红线 / AC-08）
    assert _PDF_MARKER_2 not in caplog.text


def test_acceptance_ac08_prompt_content_not_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-08 内容级缺口：chat 调用（成功 + 失败两路径）后，日志无完整 Prompt 片段。

    mock transport 注入（红线 4 允许项：测试注入点）；Prompt 独有片段若被 llm 调用路径
    任何日志语句引用，必出现于 caplog.text。alembic 迁移可能已禁用 deepseek logger →
    monkeypatch 显式恢复（ac11 同款模式）。
    """
    marker = "R1-PROMPT-MARKER-7f3a9c"
    prompt = f"请为知识点【{marker}】生成卡片（正文省略）"
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode("utf-8", errors="replace"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"cards": []}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "deepseek-v4-flash",
            },
        )

    def failing_handler(request: httpx.Request) -> httpx.Response:
        # 失败路径：上游连接错误 → deepseek logger 打 WARNING（只记异常类型名）
        raise httpx.ConnectError("simulated upstream failure", request=request)

    monkeypatch.setattr(logging.getLogger("infra.llm.deepseek"), "disabled", False)
    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    # R1 review P2-1：INFO 级捕获——成功路径若以 INFO 记录 prompt 内容也能检出（漏检窗口收窄）
    with caplog.at_level(logging.INFO):
        result = client.chat(prompt, api_key="sk-test")  # 成功路径（无日志输出）
        assert result["content"] == '{"cards": []}'
        failing = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(failing_handler))
        with pytest.raises(AppError) as excinfo:
            failing.chat(prompt, api_key="sk-test")  # 失败路径（WARNING 日志）
        assert excinfo.value.code is ErrorCode.GENERATION_FAILED
    assert marker in sent[0]  # 前置：Prompt 确实经 transport 发出（否则断言无意义）
    assert marker not in caplog.text  # 完整 Prompt 不落日志（7.1 日志红线 / AC-08）

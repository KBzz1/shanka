"""契约守卫 1b：app 路由 ↔ openapi.yaml paths 双向一致（红线 1 的路径覆盖维度）。

背景（2026-09-04 契约补齐）：既有守卫只校验已收录路径的 schema 正确性，
未守"代码端点必须全部收录"方向，曾导致 32 个端点长期缺档、/replace-pdf
旧路径滞留。本守卫以 FastAPI 自动生成的 OpenAPI 为实现事实源做双向
路径+方法集合对比。/healthz、/readyz、/metrics 为 R-04 有意排除项
（探针/指标不走业务契约，见 app/api/metrics.py 模块头）。
"""

from typing import Any

from tests.contract.support import load_openapi

_METHODS = {"get", "post", "put", "patch", "delete"}
_INTENTIONALLY_EXCLUDED = {"/healthz", "/readyz", "/metrics"}


def _app_operations() -> dict[str, set[str]]:
    from app.main import create_app

    spec: dict[str, Any] = create_app().openapi()
    return {
        path: {m for m in ops if m in _METHODS}
        for path, ops in spec["paths"].items()
        if path not in _INTENTIONALLY_EXCLUDED
    }


def _yaml_operations() -> dict[str, set[str]]:
    paths: dict[str, Any] = load_openapi()["paths"]
    return {path: {m for m in ops if m in _METHODS} for path, ops in paths.items()}


def test_openapi_paths_cover_all_app_routes() -> None:
    """实现方向：代码新增/改名端点后未同步 openapi.yaml 必须被检出。"""
    app_ops = _app_operations()
    yaml_ops = _yaml_operations()
    missing = [
        f"{m.upper()} {p}" for p, ms in app_ops.items() for m in sorted(ms - yaml_ops.get(p, set()))
    ]
    assert not missing, f"端点未收录进 openapi.yaml: {missing}"


def test_openapi_paths_have_no_stale_entries() -> None:
    """契约方向：yaml 收录了代码已删除/改名的端点（如旧 /replace-pdf）必须被检出。"""
    app_ops = _app_operations()
    yaml_ops = _yaml_operations()
    stale = [
        f"{m.upper()} {p}" for p, ms in yaml_ops.items() for m in sorted(ms - app_ops.get(p, set()))
    ]
    assert not stale, f"openapi.yaml 存在代码中不存在的端点: {stale}"

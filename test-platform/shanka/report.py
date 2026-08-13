"""统一报告:PASS/FAIL 步骤记录 + 报告字段 + 退出码(失败步骤数)。

summary() 打印后复位步骤与字段,保证 runner 多场景同进程调度时各场景计数不串。
"""

STEPS: list[tuple[str, str]] = []
META: dict[str, str] = {}


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    STEPS.append((mark, name))
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))


def record(key: str, value: object) -> None:
    """场景级报告字段(如 local_test_users_created),随 summary 打印。"""
    META[key] = str(value)


def summary() -> int:
    failed = sum(1 for mark, _ in STEPS if mark == "FAIL")
    print(f"\n{len(STEPS) - failed}/{len(STEPS)} 通过, {failed} 失败")
    for key, value in META.items():
        print(f"报告字段: {key}={value}")
    STEPS.clear()
    META.clear()
    return failed

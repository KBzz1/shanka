"""统一报告:PASS/FAIL 步骤记录 + 退出码(失败步骤数)。"""

STEPS: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    STEPS.append((mark, name))
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))


def summary() -> int:
    failed = sum(1 for mark, _ in STEPS if mark == "FAIL")
    print(f"\n{len(STEPS) - failed}/{len(STEPS)} 通过, {failed} 失败")
    return failed

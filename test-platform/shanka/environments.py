"""目标环境配置:local(本机)/ prod(生产隧道)。"""

ENVIRONMENTS: dict[str, str] = {
    "local": "http://localhost:8000",
    "prod": "https://shanka.kbzz1.top",
}


def resolve(name: str) -> str:
    if name not in ENVIRONMENTS:
        raise ValueError(f"未知环境: {name},可选 {list(ENVIRONMENTS)}")
    return ENVIRONMENTS[name]


def is_prod(name: str) -> bool:
    return name == "prod"

"""live_estimate_smoke.py:价格预估轻量冒烟(真实 DeepSeek 调用,每次验收执行一次)。

对照:services/generation/token_estimator.py 估算常量(PROMPT_TOKENS_PER_KP=1500 /
OUTPUT_TOKENS_PER_KP=3300)。3 个单知识点单元(不同难度)真实 chat,记录 prompt/output
token,均值对照常量并报告偏差;实际金额对照预估区间(price_low/price_high 同口径)。

纪律:不进 pytest 套件(自动化测试确定性零网络,LOCAL-DONE 红线);固定 3 次调用,
预算守卫 ¥0.5 封顶(保险丝);从仓库根 .env 加载真实 Key。
用法:cd main && conda run -n shanka-backend python scripts/live_estimate_smoke.py
"""

import os
import sys
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parent.parent  # main/
sys.path.insert(0, str(MAIN_DIR))
ROOT_ENV = MAIN_DIR.parent / ".env"
MAX_COST_YUAN = 0.5
SAMPLES: list[tuple[str, str, str]] = [
    ("AI Agent 定义与核心能力", "第一章 引言", "BASIC"),
    ("记忆与反思机制", "第二章 记忆", "UNDERSTANDING"),
    ("多 Agent 协作与工具调用场景", "第三章 协作", "APPLICATION"),
]


def _load_root_env() -> None:
    """仓库根 .env(DEEPSEEK_API_KEY)注入环境变量(与 scripts/run.sh 同源)。"""
    if ROOT_ENV.exists():
        for line in ROOT_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_root_env()
    from app.config import Settings
    from infra.clock import SystemClock
    from infra.llm.deepseek import DeepSeekClient
    from infra.llm.prompts import build_generation_prompt, load_asset
    from services.generation.cost import estimate_cost_by_kind
    from services.generation.token_estimator import (
        OUTPUT_TOKENS_PER_KP,
        PROMPT_TOKENS_PER_KP,
    )

    settings = Settings()
    if not settings.deepseek_api_key:
        print("缺少 DEEPSEEK_API_KEY(仓库根 .env);中止", file=sys.stderr)
        return 2
    client = DeepSeekClient(settings, api_key=settings.deepseek_api_key)
    prompt_asset = load_asset("prompts", "generator")
    card_schema = load_asset("schemas", "card")

    print("=== 价格预估 live 冒烟(单知识点单元 x3,真实调用)===")
    total_prompt = 0
    total_output = 0
    for topic, chapter, difficulty in SAMPLES:
        prompt = build_generation_prompt(
            prompt_asset,
            topic=topic,
            chapter_name=chapter,
            difficulty=difficulty,
            custom_requirements=None,
            card_schema=card_schema,
        )
        result = client.chat(prompt)  # 明文 Key 构造时注入(executor 同款)
        usage = result["usage"]
        hit: int = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss: int = int(usage.get("prompt_cache_miss_tokens") or 0)
        out: int = int(usage.get("completion_tokens") or 0)
        total_prompt += hit + miss
        total_output += out
        print(f"{difficulty:<13} prompt={hit + miss:>5}(hit {hit}/miss {miss}) output={out:>5}")

    avg_prompt = total_prompt / len(SAMPLES)
    avg_output = total_output / len(SAMPLES)
    effective_date = SystemClock().now_utc().date().isoformat()
    # 实际金额:3 次均为新样本首调,保守按全 miss 口径
    actual = estimate_cost_by_kind(0, total_prompt, total_output, effective_date=effective_date)
    low = estimate_cost_by_kind(total_prompt, 0, total_output, effective_date=effective_date)
    print(
        f"均值 prompt={avg_prompt:.0f}(常量 {PROMPT_TOKENS_PER_KP}) "
        f"output={avg_output:.0f}(常量 {OUTPUT_TOKENS_PER_KP})"
    )
    print(
        f"金额:实际(全 miss)¥{actual['total']:.4f} 区间 ¥{low['total']:.4f}~¥{actual['total']:.4f}"
    )
    if actual["total"] > MAX_COST_YUAN:
        print(f"预算超限 ¥{actual['total']:.4f} > ¥{MAX_COST_YUAN};中止", file=sys.stderr)
        return 2
    prompt_drift = (avg_prompt - PROMPT_TOKENS_PER_KP) / PROMPT_TOKENS_PER_KP
    output_drift = (avg_output - OUTPUT_TOKENS_PER_KP) / OUTPUT_TOKENS_PER_KP
    print(f"偏差:prompt {prompt_drift:+.1%} output {output_drift:+.1%}")
    if abs(prompt_drift) > 0.2 or abs(output_drift) > 0.2:
        print("提示:偏差 >20%,评估是否校准 token_estimator 常量(离线校准,登记后修改)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

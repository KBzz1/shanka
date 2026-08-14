"""成本护栏:运行前最坏调用预算推导 + 运行后批次对账(废弃「live 固定 3 次调用」假设)。

预算推导(DESIGN 8.3):受控 fixture(章节/单元配置)+ 契约默认上限推导最坏调用数与
token 上限;黑盒客户端无法读取后端 Settings,上限按契约默认值镜像(来源
docs/Architecture/structure-contract.md 3.7/§6.2/§8 与 main/app/config.py 默认值,
后端运维调整时此处需同步)。

对账边界:后端无 llm_call_attempts GET 端点;GENERATING 阶段经 GET /tasks/{id}/batches
观测(批=单元,retry_count/token/cost_estimate 均为账本投影),PLANNING/SCORING 阶段
尝试数无 HTTP 观测入口——对账只覆盖 GENERATING,边界如实声明(见 reconcile)。
"""

from dataclasses import dataclass
from typing import Any

THRESHOLD = 3  # 最坏调用数超过此数(> 3)必须 --confirm-cost

# ---- 契约镜像常量(structure-contract 3.7/§6.2/§8 + config.py 默认值;保守推导) ----
_UNITS_PER_CHAPTER = 3  # 每章基础单元预算
_DENSITY_FACTOR = {"COMPACT": 1, "BALANCED": 2, "EXTENSIVE": 3}  # 3.7 密度系数
_PLANNING_RETRY_LIMIT = 2  # 规划每组预算 2 次重试(共 3 次尝试)
_GENERATION_RETRY_LIMIT = 2  # 批次 Schema 校验失败重试上限(共 3 次尝试)
_CARDS_PER_UNIT = 1  # 3.7:批=单元,每单元 1 卡
_MAX_SCORING_CALLS_PER_TASK = 60  # 评分调用数全局上限
_FIXED_CALLS = 2  # PUT /api-key 校验 1 + POST /samples 1(固定真实调用)
# 单次输出 token 上限(config.py;可运维调整)
_PLANNER_MAX_OUTPUT_TOKENS = 2048
_GENERATOR_MAX_OUTPUT_TOKENS = 768
_SCORING_MAX_OUTPUT_TOKENS = 4096
# 输入字符上限保守映射 1 字符 ≈ 1 token(中文最坏上界;用于最坏成本估算)
_PLANNER_MAX_INPUT_CHARS = 20_000
_GENERATOR_MAX_INPUT_CHARS = 10_000
_SCORING_MAX_INPUT_CHARS = 15_000
# 单价镜像(8.4 价格常量,2026-08-11 起;闸门展示用,对账以服务端 cost_estimate 为准)
_PRICE_CACHE_MISS_PER_TOKEN = 2.0 / 1_000_000
_PRICE_OUTPUT_PER_TOKEN = 8.0 / 1_000_000


@dataclass(frozen=True)
class Budget:
    """一次 live 生成任务的最坏调用预算(含重试上限,最坏 = 每调用都打满重试)。"""

    units: int  # 最坏规划单元数(章节 × 每章基础预算 × 密度系数)
    planning_calls: int  # 规划:1 规划组 × (1+重试上限)——前提:前 2 章页文本 ≤ 20k,超出则拆组、实际调用高于此推导值
    generation_calls: int  # 生成:批=单元,每批 (1+重试上限)
    scoring_calls: int  # 评分:每单元 1 卡,APPLICATION 逐单元最坏 = 单元数(封顶 60)
    fixed_calls: int  # 固定真实调用:api-key 校验 + samples
    planner_output_tokens: int
    generator_output_tokens: int
    scoring_output_tokens: int

    def total_calls(self) -> int:
        return (
            self.planning_calls + self.generation_calls + self.scoring_calls + self.fixed_calls
        )

    def worst_output_tokens(self) -> int:
        return (
            self.planning_calls * self.planner_output_tokens
            + self.generation_calls * self.generator_output_tokens
            + self.scoring_calls * self.scoring_output_tokens
        )

    def worst_input_tokens(self) -> int:
        return (
            self.planning_calls * _PLANNER_MAX_INPUT_CHARS
            + self.generation_calls * _GENERATOR_MAX_INPUT_CHARS
            + self.scoring_calls * _SCORING_MAX_INPUT_CHARS
        )

    def worst_cost_yuan(self) -> float:
        """最坏成本估算(元):输入按 cache_miss、输出按 output 单价,只作闸门展示。"""
        return round(
            self.worst_input_tokens() * _PRICE_CACHE_MISS_PER_TOKEN
            + self.worst_output_tokens() * _PRICE_OUTPUT_PER_TOKEN,
            6,
        )


def derive_budget(*, chapters: int, quantity_tendency: str, generate: bool) -> Budget:
    """按受控 fixture 推导最坏调用预算;generate=False(skip-generate)时管线三阶段为 0。"""
    density = _DENSITY_FACTOR.get(quantity_tendency)
    if density is None:
        raise ValueError(f"未知 quantity_tendency: {quantity_tendency}")
    units = chapters * _UNITS_PER_CHAPTER * density if generate else 0
    # 1 规划组前提(欠报方向声明):前 2 章累计页文本 ≤ planner_max_input_chars 20k
    # (config.py);超过时后端按 20k 拆组(max_planner_groups_per_task=30),实际 PLANNING
    # 调用与成本高于此推导值(最坏 = 组数 × (1+重试上限));fixture 未锚定页文本量,调整需同步声明
    planning = 1 + _PLANNING_RETRY_LIMIT if generate else 0
    generation = units * (1 + _GENERATION_RETRY_LIMIT)
    scoring = min(units * _CARDS_PER_UNIT, _MAX_SCORING_CALLS_PER_TASK)
    return Budget(
        units=units,
        planning_calls=planning,
        generation_calls=generation,
        scoring_calls=scoring,
        fixed_calls=_FIXED_CALLS,  # api-key 校验 + samples 在 skip-generate 前固定执行
        planner_output_tokens=_PLANNER_MAX_OUTPUT_TOKENS,
        generator_output_tokens=_GENERATOR_MAX_OUTPUT_TOKENS,
        scoring_output_tokens=_SCORING_MAX_OUTPUT_TOKENS,
    )


def budget_for(mod: Any) -> Budget | None:
    """场景模块声明 BUDGET_FIXTURE 时推导其预算;0 LLM 场景返回 None。"""
    fixture = getattr(mod, "BUDGET_FIXTURE", None)
    if not isinstance(fixture, dict):
        return None
    return derive_budget(**fixture)


def describe(budget: Budget) -> str:
    """预算明细单行(闸门拒绝消息用)。"""
    return (
        f"PLANNING {budget.planning_calls} + GENERATING {budget.generation_calls} "
        f"+ SCORING {budget.scoring_calls} + 固定 {budget.fixed_calls} = "
        f"{budget.total_calls()} 次调用;最坏输出 token {budget.worst_output_tokens()}, "
        f"最坏输入 token {budget.worst_input_tokens()};最坏成本 ≈ ¥{budget.worst_cost_yuan():.2f}"
    )


def aggregate(scenarios: list[Any]) -> int:
    return sum(int(getattr(s, "LLM_CALLS", 0)) for s in scenarios)


def requires_confirm(total: int) -> bool:
    return total > THRESHOLD


def _batch_attempts(batch: dict) -> int:
    """单批账本尝试数投影:SUCCEEDED 时 retry_count=失败数(+1);
    FAILED/SKIPPED 时 retry_count=账本尝试数(attempt_no,不再 +1)。"""
    status = str(batch.get("status") or "")
    retry = int(batch.get("retry_count") or 0)
    if status == "SUCCEEDED":
        return retry + 1
    return max(retry, 1)  # 失败/跳过批 retry_count ≥ 1;防御性下限防异常数据计 0


@dataclass
class Reconciliation:
    """GENERATING 阶段对账结果(批=单元投影;PLANNING/SCORING 无 HTTP 观测入口)。"""

    batch_count: int  # 实际批数(= 实际单元数,批=单元)
    generation_attempts: int  # Σ 批账本尝试数投影
    generation_budget: int  # 预算生成调用数
    unit_budget: int  # 预算单元数
    tokens: int  # Σ(cache_hit+cache_miss+output),None 计 0
    cost_yuan: float  # Σ 服务端 cost_estimate(8.4 常量,权威),缺失计 0
    within_budget: bool  # 尝试数与批数均 ≤ 预算
    within_unit_budget: bool

    @property
    def usage_line(self) -> str:
        return (
            f"批次 {self.batch_count}/{self.unit_budget}, 生成尝试 "
            f"{self.generation_attempts}/{self.generation_budget}, token {self.tokens}, "
            f"成本 ¥{self.cost_yuan:.6f}(服务端估算)"
        )


def reconcile(batches: list[dict], budget: Budget) -> Reconciliation:
    """运行后对账:GET /tasks/{id}/batches 响应 → 实际尝试/token/成本 vs 预算。

    边界:后端无 llm_call_attempts GET 端点;batches 只覆盖 GENERATING 阶段
    (openapi 3.7:尝试数与 token 权威在账本,Batch 列为生成阶段兼容投影),
    PLANNING/SCORING 尝试数不参与对账,报告需如实声明。
    """
    batch_count = len(batches)
    attempts = sum(_batch_attempts(b) for b in batches)
    tokens = sum(
        int(b.get("cache_hit_tokens") or 0)
        + int(b.get("cache_miss_tokens") or 0)
        + int(b.get("output_tokens") or 0)
        for b in batches
    )
    cost_yuan = round(sum(float(b.get("cost_estimate") or 0.0) for b in batches), 6)
    within_unit_budget = batch_count <= budget.units
    return Reconciliation(
        batch_count=batch_count,
        generation_attempts=attempts,
        generation_budget=budget.generation_calls,
        unit_budget=budget.units,
        tokens=tokens,
        cost_yuan=cost_yuan,
        within_budget=attempts <= budget.generation_calls and within_unit_budget,
        within_unit_budget=within_unit_budget,
    )

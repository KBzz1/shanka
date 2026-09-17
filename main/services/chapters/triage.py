"""triage.py：章节结构确定性分诊（V25-D-38，程序优先、模型其次）。

统一分诊规则（全资料类型）：

1. 自带结构直用：PDF outline（source=TOC）/ HTML 标题（source=HEADING）/
   ZIP 文件夹（source=ZIP）——结构候选由各格式适配器产出；
2. 总字符 ≤ ``single_chapter_max_chars`` → 直接单章（source=AUTO，零模型调用，
   不要求已存 API Key）——阈值锚点：对齐粗规划单段输入，更小资料的章节结构
   对生成质量零影响，只影响用户选范围；
3. 同步类型（HTML）无标题结构 → 恒单章（source=AUTO），不论大小；
4. 仅"无目录且超阈值"的 PDF 走 V25-D-36 AI 规划兜底（异步基础设施所在）。

纯函数模块，无 DB/IO。
"""

from app.config import Settings

AUTO = "AUTO"


def is_single_chapter_by_size(total_chars: int, settings: Settings) -> bool:
    """分诊规则 2：总字符 ≤ 阈值 → 单章。"""
    return total_chars <= settings.single_chapter_max_chars

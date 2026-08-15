"""UserPreferences（structure-contract 3.15；V2.5 新增）。

覆盖深度/整数比例/每日目标默认值按账号跨设备保存；比例语义：
三档为 0~100 的 10% 整数档、合计 100、允许任一档为 0（比例全 0 为非法配置）。
"""

DEFAULT_COVERAGE_MODE = "BALANCED"
DEFAULT_DIFFICULTY_RATIO = {"basic": 40, "understanding": 40, "deep_question": 20}
DEFAULT_DAILY_LEARNING_GOAL = 50
DEFAULT_LEARNING_TIMEZONE = "Asia/Shanghai"  # 账号行首次创建时的服务端默认（PRD V25-SET-FR-04：首次设置由客户端建议设备时区后 PATCH 覆盖）
DIFFICULTY_RATIO_TOTAL = 100

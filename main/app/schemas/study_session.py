"""学习会话 schema（openapi StudySession/StudySessionBeginRequest/StudySessionReportRequest；
structure-contract 3.25，V25-D-37）。

薄容器：只记来源、范围、学习日与累计秒数，不保存卡片状态/队列/完成数。origin 用 str
（非 Literal）：非法值由 service 内校验抛 VALIDATION_ERROR（400），与
ReviewEventRequest.rating 同口径。
"""

from pydantic import BaseModel

from app.schemas.study_plan import TodayPlanCard


class StudySession(BaseModel):
    session_id: str
    origin: str  # PLAN/BACKLOG/ADHOC（与会话 begin 校验同枚举）
    deck_id: str | None  # ADHOC 必填且归属当前用户；PLAN/BACKLOG 恒为 None
    study_date: str  # 账号学习时区下的学习日期（自然键成员）
    study_seconds: int  # 累计秒数；绝对值上报，服务端 max 合并
    started_at: str
    last_reported_at: str | None = None
    ended_at: str | None = None  # 客户端申报结束；缺失不阻断同日续用


class StudySessionBeginRequest(BaseModel):
    origin: str  # PLAN/BACKLOG/ADHOC（service 内校验 → 400 VALIDATION_ERROR）
    deck_id: str | None = None  # 仅 ADHOC 必填；PLAN/BACKLOG 携带则 400
    # V25-D-37 会话重置：true 时封存当日同源同范围活跃会话并开新行（时长从 0 计），
    # 且响应携带 reset_review_queue 的全卡组复盘队列；仅 ADHOC 支持携带。
    reset: bool = False


class StudySessionBeginResponse(BaseModel):
    """begin 响应：会话本体 + 仅 reset=True 时携带的全卡组复盘队列（cards，可为空列表）。"""

    session_id: str
    origin: str
    deck_id: str | None
    study_date: str
    study_seconds: int
    started_at: str
    last_reported_at: str | None = None
    ended_at: str | None = None
    cards: list[TodayPlanCard] = []


class StudySessionReportRequest(BaseModel):
    study_seconds: int  # 本次会话累计绝对秒数（非增量）；服务端按 max 合并
    ended: bool = False

"""账号 schema 锚点（openapi AuthRegisterRequest/AuthLoginRequest/AuthUser/AuthMeUpdateRequest/AuthSessionResponse）。

V2.5：AuthUser 含 email（只读，不可 PATCH）与 avatar_key（mood_01~mood_12 预设头像）；
PATCH /auth/me 仅接受 { username?, avatar_key? }，至少一个字段。
"""

from pydantic import BaseModel, Field, model_validator


class AuthRegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class AuthUser(BaseModel):
    user_id: str
    username: str
    email: str  # V2.5 只读返回规范化后的当前登录邮箱；不可 PATCH
    avatar_key: str  # V2.5 预设头像 mood_01~mood_12；默认 mood_01
    created_at: str


class AuthMeUpdateRequest(BaseModel):
    """更新昵称或预设头像（openapi AuthMeUpdateRequest；V2.5：至少一个字段，email 只读）。"""

    username: str | None = Field(default=None, min_length=1, max_length=24)
    avatar_key: str | None = None  # mood_01~mood_12（服务层按 AvatarKey 枚举校验）

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "AuthMeUpdateRequest":
        if self.username is None and self.avatar_key is None:
            raise ValueError("至少提供一个字段（username 或 avatar_key）")
        return self


class AuthSessionResponse(BaseModel):
    user: AuthUser
    access_token: str
    token_type: str
    expires_at: str

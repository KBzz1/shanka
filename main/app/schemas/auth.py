"""账号 schema 锚点（openapi 2.2.0 AuthRegisterRequest/AuthLoginRequest/AuthUser/AuthSessionResponse）。"""

from pydantic import BaseModel, Field


class AuthRegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class AuthUser(BaseModel):
    user_id: str
    username: str
    created_at: str


class AuthSessionResponse(BaseModel):
    user: AuthUser
    access_token: str
    token_type: str
    expires_at: str

"""认证相关的请求与响应结构。"""

from __future__ import annotations
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64, description="用户名")
    password: str = Field(min_length=1, max_length=256, description="口令")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256, description="新口令，至少 12 位")


class PermissionOut(BaseModel):
    code: str
    name_zh: str
    category: str


class UserProfileOut(BaseModel):
    id: str
    username: str
    display_name: str
    email: EmailStr
    tenant_id: str | None = Field(default=None, description="为空表示平台方（内部人员）")
    is_protected: bool = Field(description="是否固定管理员")
    must_change_password: bool
    roles: list[str] = Field(default_factory=list, description="角色码列表")
    permissions: list[str] = Field(default_factory=list, description="有效权限码列表")


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="令牌有效期（秒）")
    must_change_password: bool
    user: UserProfileOut


class SessionInfoOut(BaseModel):
    user: UserProfileOut
    last_login_at: datetime | None = None

"""管理端请求与响应结构。"""

from __future__ import annotations
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class TenantCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$",
                      description="租户标识，英文小写，如 demo-corp")
    name: str = Field(min_length=1, max_length=200, description="企业名称")
    contact_email: EmailStr | None = None
    subscribed_domains: list[str] = Field(default_factory=lambda: ["finance_tax"])
    quota_settings: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "suspended"] | None = None
    contact_email: EmailStr | None = None
    subscribed_domains: list[str] | None = None
    quota_settings: dict[str, Any] | None = None
    note: str | None = None


class TenantOut(BaseModel):
    id: str
    code: str
    name: str
    status: str
    subscribed_domains: list[str]
    quota_settings: dict[str, Any]
    contact_email: str | None = None
    note: str | None = None
    created_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=12, max_length=256,
                                 description="留空则由系统生成随机初始口令")
    role_code: str = Field(description="要授予的角色码：admin / reviewer / client")
    tenant_id: str | None = Field(default=None,
                                  description="客户角色必填；审核专家可留空表示平台方")
    must_change_password: bool = True


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None
    must_change_password: bool | None = None


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    display_name: str
    tenant_id: str | None
    is_protected: bool
    is_active: bool
    must_change_password: bool
    roles: list[str] = Field(default_factory=list)
    last_login_at: datetime | None = None
    created_at: datetime


class RoleAssign(BaseModel):
    role_code: str = Field(description="要授予的角色码")


class PermissionGrant(BaseModel):
    permission_code: str = Field(description="要授予或收回的权限码")
    effect: Literal["allow", "deny"] = "allow"
    reason: str | None = Field(default=None, max_length=500, description="授权原因，写入审计")


class GrantResultOut(BaseModel):
    user_id: str
    permission_code: str
    effect: str
    applied: bool = Field(description="True=本次改变了权限；False=原本就是这个状态")


class AuditLogOut(BaseModel):
    id: int
    occurred_at: datetime
    actor_id: str | None
    actor_username: str | None
    tenant_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, Any]
    ip_address: str | None

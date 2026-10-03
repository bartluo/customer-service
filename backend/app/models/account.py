"""账号、租户、角色、权限、审计日志的 ORM 模型。

对应技术方案 v3.3 第 10.1 节与 11.1／11.4／11.5 节。

几条硬约束（写进数据库，不靠应用层自觉）：
  · 固定管理员只能有一个：users 上用部分唯一索引 is_protected = true
  · 内置三个角色不可删除：roles.is_builtin = true
  · 审计日志只增不改：应用层不提供删除接口，数据库不给 UPDATE/DELETE 权限
"""

from __future__ import annotations
from datetime import datetime
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tenant(Base, TimestampMixin):
    """租户：一个客户企业的独立空间，数据彼此隔离。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # 订阅了哪些域，如 ["finance_tax"]
    subscribed_domains: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # 配额：单租户的问答次数上限等，留空表示不限
    quota_settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base, TimestampMixin):
    """用户：能登录系统的人。

    is_protected = True 即固定管理员：唯一、不可删除、不可停用、不可降权。
    平台方人员（内部审核专家）也属于用户，但 tenant_id 为空。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # 归属租户；为空表示平台方（内部人员）
    tenant_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )

    is_protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 首次登录必须改密码（固定管理员与管理员创建的账号都设 true）
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant | None] = relationship(back_populates="users")
    user_roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    user_permissions: Mapped[list["UserPermission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # 固定管理员唯一：数据库层面保证不会出现第二个
        Index(
            "uq_users_single_protected",
            "is_protected",
            unique=True,
            # 部分唯一索引：只对 is_protected = true 的行生效，保证固定管理员唯一
            postgresql_where=text("is_protected = true"),
        ),
    )


class Role(Base, TimestampMixin):
    """角色：权限的打包模板。财税首版三个内置角色 + 后续动态新增。"""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name_zh: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 内置角色不可删除（技术方案 10.1）
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 是否可被普通管理员授予。审核专家角色为 False，只有固定管理员能给（11.4 两级授权）
    grantable_by_admin: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    role_permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class Permission(Base, TimestampMixin):
    """权限项：一个具体动作，如 knowledge.review。默认拒绝（表里没有的都不放行）。"""

    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name_zh: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 普通管理员能否把该权限授予他人（技术方案 10.5 表格最后一列）
    delegable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 该权限涉及客户敏感数据，默认只允许审核专家在复核流程中看到
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RolePermission(Base):
    """角色 → 权限。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )

    role: Mapped[Role] = relationship(back_populates="role_permissions")
    permission: Mapped[Permission] = relationship()


class UserRole(Base):
    """用户 → 角色。

    scope 字段：客户角色必须与用户所属租户一致（由应用层校验）；
    平台方角色（审核专家）scope 为 "platform"。
    """

    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    granted_by: Mapped[str] = mapped_column(PGUUID(as_uuid=False), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="user_roles")
    role: Mapped[Role] = relationship()


class UserPermission(Base):
    """用户级权限覆盖：在角色之上单独加权限（allow）或收权限（deny）。

    为什么需要：企业老板和会计都是"客户"角色，但会计要能保存计算底稿。
    为此新造一个角色反而更乱，用用户级授权表达更准确。
    """

    __tablename__ = "user_permissions"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    permission_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )
    effect: Mapped[str] = mapped_column(String(10), nullable=False)  # "allow" | "deny"
    reason: Mapped[str | None] = mapped_column(Text)
    granted_by: Mapped[str] = mapped_column(PGUUID(as_uuid=False), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="user_permissions")
    permission: Mapped[Permission] = relationship()

    __table_args__ = (UniqueConstraint("user_id", "permission_id", name="uq_user_permission"),)


class AuditLog(Base):
    """审计日志：只增不改。

    记录登录、授权、知识变更、导出、高风险放行。财税系统要能自证"谁做的决定"，
    所以这张表不提供更新与删除能力（应用层无接口，数据库账号只授予 INSERT/SELECT）。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False))
    actor_username: Mapped[str | None] = mapped_column(String(64))
    tenant_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100))
    # 变更前后的关键字段摘要；不记录敏感明文（如口令、令牌）
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (Index("ix_audit_actor_action", "actor_id", "action"),)

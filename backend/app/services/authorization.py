"""权限判定与授权操作。

核心规则（技术方案 10.1 / 10.4 / 10.5）：
  · 权限 = 角色带来的权限 + 用户级 allow，再减去用户级 deny
  · 固定管理员拥有全部权限，且不可被任何 deny 收权
  · 普通管理员只能授予 delegable=True 的权限
  · 审核专家角色（grantable_by_admin=False）只能由固定管理员授予
"""

from __future__ import annotations
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.permissions import PERMISSIONS, ROLES
from app.models import Permission, Role, RolePermission, User, UserPermission, UserRole


class PermissionDenied(Exception):
    """当前用户没有该权限。调用方返回 403。"""


class ProtectedAdminViolation(Exception):
    """试图删除、停用或降权固定管理员。调用方返回 403。"""


@dataclass(frozen=True)
class Principal:
    """已认证的调用者及其权限集合。"""

    user_id: str
    username: str
    tenant_id: str | None
    is_protected: bool
    permissions: frozenset[str]
    role_codes: tuple[str, ...]

    def has(self, permission_code: str) -> bool:
        if self.is_protected:
            return True
        return permission_code in self.permissions

    def require(self, permission_code: str) -> None:
        if not self.has(permission_code):
            raise PermissionDenied(permission_code)


def _delegable_codes() -> frozenset[str]:
    return frozenset(spec.code for spec in PERMISSIONS if spec.delegable)


def load_principal(db: Session, user: User) -> Principal:
    """从数据库读取用户的有效权限集合。

    每次请求都调用：权限变更必须立刻生效，不能等令牌过期（见 security/tokens.py 说明）。
    """

    if user.is_protected:
        # 固定管理员：全部权限，无需查库计算
        return Principal(
            user_id=user.id,
            username=user.username,
            tenant_id=user.tenant_id,
            is_protected=True,
            permissions=frozenset(),
            role_codes=(),
        )

    role_rows = db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, Role.is_active.is_(True))
    ).scalars().all()

    role_codes = tuple(role_rows)
    if not role_codes:
        granted: set[str] = set()
    else:
        granted = set(
            db.execute(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id.in_(
                    select(UserRole.role_id).where(UserRole.user_id == user.id)
                ))
            ).scalars().all()
        )

    overrides = db.execute(
        select(Permission.code, UserPermission.effect)
        .join(UserPermission, UserPermission.permission_id == Permission.id)
        .where(UserPermission.user_id == user.id)
    ).all()
    for code, effect in overrides:
        if effect == "allow":
            granted.add(code)
        else:
            granted.discard(code)

    return Principal(
        user_id=user.id,
        username=user.username,
        tenant_id=user.tenant_id,
        is_protected=False,
        permissions=frozenset(granted),
        role_codes=role_codes,
    )


def assert_can_delegate(actor: Principal, permission_code: str) -> None:
    """普通管理员只能转授 delegable 的权限；固定管理员不受限。"""

    if actor.is_protected:
        return
    if permission_code not in _delegable_codes():
        raise PermissionDenied(permission_code)


def assert_can_assign_role(actor: Principal, role: Role) -> None:
    """审核专家角色只有固定管理员能授。"""

    if actor.is_protected:
        return
    if not role.grantable_by_admin:
        raise PermissionDenied(f"role:{role.code}")


def assert_target_mutable(target: User) -> None:
    """固定管理员不可被删除、停用、降权。"""

    if target.is_protected:
        raise ProtectedAdminViolation(target.username)

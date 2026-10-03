"""管理端接口：租户、用户、授权、审计。

权限约定（技术方案 10.4 两级授权）：
  · 普通管理员：建租户、建客户账号、授予 delegable 权限
  · 固定管理员：额外可授"审核专家"角色与不可转授的权限
  · 固定管理员自身不可被删除、停用、降权
"""

from __future__ import annotations
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dependencies import CurrentPrincipal, DatabaseSession, require_permission
from app.domain.permissions import (
    PERMISSIONS,
    ROLES,
    TENANT_MANAGE,
    USER_MANAGE,
)
from app.models import (
    AuditLog,
    Permission,
    Role,
    Tenant,
    User,
    UserPermission,
    UserRole,
)
from app.schemas.admin import (
    AuditLogOut,
    GrantResultOut,
    PermissionGrant,
    RoleAssign,
    TenantCreate,
    TenantOut,
    TenantUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.security.passwords import generate_password, hash_password, password_problems
from app.services import audit
from app.services.authorization import (
    PermissionDenied,
    ProtectedAdminViolation,
    assert_can_assign_role,
    assert_can_delegate,
    assert_target_mutable,
)

router = APIRouter(prefix="/admin", tags=["管理端"])

require_tenant_manage = require_permission(TENANT_MANAGE)
require_user_manage = require_permission(USER_MANAGE)


def _ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _tenant_out(tenant: Tenant) -> TenantOut:
    return TenantOut(
        id=tenant.id,
        code=tenant.code,
        name=tenant.name,
        status=tenant.status,
        subscribed_domains=list(tenant.subscribed_domains or []),
        quota_settings=dict(tenant.quota_settings or {}),
        contact_email=tenant.contact_email,
        note=tenant.note,
        created_at=tenant.created_at,
    )


def _user_roles(db: Session, user_id: str) -> list[str]:
    return list(
        db.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
            .order_by(Role.code)
        ).scalars().all()
    )


def _user_out(db: Session, user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        tenant_id=user.tenant_id,
        is_protected=user.is_protected,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        roles=_user_roles(db, user.id),
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


# ---------------- 租户 ----------------
@router.get("/tenants", summary="租户列表", dependencies=[Depends(require_tenant_manage)])
def list_tenants(db: DatabaseSession) -> list[TenantOut]:
    tenants = db.execute(select(Tenant).order_by(Tenant.created_at)).scalars().all()
    return [_tenant_out(item) for item in tenants]


@router.post("/tenants", summary="创建租户", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_tenant_manage)])
def create_tenant(
    payload: TenantCreate, request: Request, db: DatabaseSession, principal: CurrentPrincipal
) -> TenantOut:
    exists = db.execute(select(Tenant).where(Tenant.code == payload.code)).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="租户标识已存在")
    tenant = Tenant(
        code=payload.code,
        name=payload.name,
        contact_email=payload.contact_email,
        subscribed_domains=payload.subscribed_domains,
        quota_settings=payload.quota_settings,
        note=payload.note,
    )
    db.add(tenant)
    db.flush()
    audit.record(
        db,
        action="tenant.created",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=tenant.id,
        target_type="tenant",
        target_id=tenant.id,
        detail={"code": tenant.code, "name": tenant.name},
        ip_address=_ip(request),
    )
    db.commit()
    return _tenant_out(tenant)


@router.patch("/tenants/{tenant_id}", summary="修改租户",
              dependencies=[Depends(require_tenant_manage)])
def update_tenant(
    tenant_id: str, payload: TenantUpdate, request: Request,
    db: DatabaseSession, principal: CurrentPrincipal,
) -> TenantOut:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在")
    changes: dict[str, Any] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if getattr(tenant, field) != value:
            changes[field] = {"from": getattr(tenant, field), "to": value}
        setattr(tenant, field, value)
    audit.record(
        db,
        action="tenant.updated",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=tenant.id,
        target_type="tenant",
        target_id=tenant.id,
        detail=changes,
        ip_address=_ip(request),
    )
    db.commit()
    return _tenant_out(tenant)


# ---------------- 角色与权限字典 ----------------
@router.get("/roles", summary="角色列表（含每个角色的权限）")
def list_roles(db: DatabaseSession) -> list[dict[str, Any]]:
    from app.models import RolePermission

    result = []
    for spec in ROLES:
        role = db.execute(select(Role).where(Role.code == spec.code)).scalar_one_or_none()
        if role is None:
            continue
        codes = list(
            db.execute(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
                .order_by(Permission.code)
            ).scalars().all()
        )
        result.append(
            {
                "code": role.code,
                "name_zh": role.name_zh,
                "description": role.description,
                "is_builtin": role.is_builtin,
                "grantable_by_admin": role.grantable_by_admin,
                "permissions": codes,
            }
        )
    return result


@router.get("/permissions", summary="权限项清单")
def list_permissions(db: DatabaseSession) -> list[dict[str, Any]]:
    rows = db.execute(select(Permission).order_by(Permission.category, Permission.code)).scalars().all()
    return [
        {
            "code": row.code,
            "name_zh": row.name_zh,
            "category": row.category,
            "delegable": row.delegable,
            "sensitive": row.sensitive,
            "description": row.description,
        }
        for row in rows
    ]


# ---------------- 用户 ----------------
@router.get("/users", summary="用户列表", dependencies=[Depends(require_user_manage)])
def list_users(
    db: DatabaseSession,
    tenant_id: str | None = Query(default=None, description="按租户过滤；留空返回全部"),
) -> list[UserOut]:
    stmt = select(User)
    if tenant_id:
        stmt = stmt.where(User.tenant_id == tenant_id)
    users = db.execute(stmt.order_by(User.created_at)).scalars().all()
    return [_user_out(db, user) for user in users]


@router.post("/users", summary="创建用户并授予角色", status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_user_manage)])
def create_user(
    payload: UserCreate, request: Request, db: DatabaseSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    if db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")
    if db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已被使用")

    role = db.execute(select(Role).where(Role.code == payload.role_code)).scalar_one_or_none()
    if role is None or not role.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="角色不存在或已停用")
    try:
        assert_can_assign_role(principal, role)
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"该角色只能由固定管理员授予：{role.code}",
        ) from exc

    # 客户角色必须归属某个租户；审核专家与管理员属于平台方（tenant_id 为空）
    if role.code == "client":
        if not payload.tenant_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="客户角色必须指定租户")
        if db.get(Tenant, payload.tenant_id) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="租户不存在")
    elif payload.tenant_id and db.get(Tenant, payload.tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="租户不存在")

    password = payload.password
    generated = False
    if not password:
        password = generate_password()
        generated = True
    else:
        problems = password_problems(password)
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="；".join(problems)
            )

    user = User(
        username=payload.username,
        email=payload.email,
        display_name=payload.display_name,
        password_hash=hash_password(password),
        tenant_id=payload.tenant_id,
        is_protected=False,
        is_active=True,
        must_change_password=payload.must_change_password,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id, granted_by=principal.user_id))
    audit.record(
        db,
        action="user.created",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=user.tenant_id,
        target_type="user",
        target_id=user.id,
        detail={
            "username": user.username,
            "role": role.code,
            "initial_password_generated": generated,
        },
        ip_address=_ip(request),
    )
    db.commit()
    return {
        "user": _user_out(db, user).model_dump(mode="json"),
        # 初始口令只在这一次返回，数据库里只有哈希，之后无法再查
        "initial_password": password if generated else None,
    }


@router.patch("/users/{user_id}", summary="修改用户（停用、改名等）",
              dependencies=[Depends(require_user_manage)])
def update_user(
    user_id: str, payload: UserUpdate, request: Request,
    db: DatabaseSession, principal: CurrentPrincipal,
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_active") is False or changes.get("must_change_password") is False:
        try:
            assert_target_mutable(user)
        except ProtectedAdminViolation as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="固定管理员不可停用或降权",
            ) from exc
    for field, value in changes.items():
        setattr(user, field, value)
    audit.record(
        db,
        action="user.updated",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=user.tenant_id,
        target_type="user",
        target_id=user.id,
        detail=changes,
        ip_address=_ip(request),
    )
    db.commit()
    return _user_out(db, user)


@router.delete("/users/{user_id}", summary="删除用户（固定管理员除外）",
               dependencies=[Depends(require_user_manage)])
def delete_user(
    user_id: str, request: Request, db: DatabaseSession, principal: CurrentPrincipal
) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    try:
        assert_target_mutable(user)
    except ProtectedAdminViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="固定管理员不可删除"
        ) from exc
    snapshot = {"username": user.username, "roles": _user_roles(db, user.id)}
    tenant_id = user.tenant_id
    db.delete(user)
    audit.record(
        db,
        action="user.deleted",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=tenant_id,
        target_type="user",
        target_id=user_id,
        detail=snapshot,
        ip_address=_ip(request),
    )
    db.commit()
    return {"message": "用户已删除"}


@router.post("/users/{user_id}/reset-password", summary="重置他人口令",
             dependencies=[Depends(require_user_manage)])
def reset_user_password(
    user_id: str, request: Request, db: DatabaseSession, principal: CurrentPrincipal
) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.is_protected and not principal.is_protected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有固定管理员能重置固定管理员的口令")
    password = generate_password()
    user.password_hash = hash_password(password)
    user.must_change_password = True
    audit.record(
        db,
        action="user.password_reset",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=user.tenant_id,
        target_type="user",
        target_id=user.id,
        detail={"username": user.username},
        ip_address=_ip(request),
    )
    db.commit()
    return {"message": "口令已重置", "initial_password": password}


# ---------------- 授权 ----------------
@router.post("/users/{user_id}/roles", summary="给用户授予角色",
             dependencies=[Depends(require_user_manage)])
def assign_role(
    user_id: str, payload: RoleAssign, request: Request,
    db: DatabaseSession, principal: CurrentPrincipal,
) -> dict[str, Any]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.is_protected and not principal.is_protected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权改动固定管理员的角色")
    role = db.execute(select(Role).where(Role.code == payload.role_code)).scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="角色不存在")
    try:
        assert_can_assign_role(principal, role)
    except PermissionDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"该角色只能由固定管理员授予：{role.code}",
        ) from exc
    if role.code == "client" and not user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="客户角色必须先给用户指定租户"
        )
    exists = db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role.id)
    ).scalar_one_or_none()
    applied = exists is None
    if applied:
        db.add(UserRole(user_id=user_id, role_id=role.id, granted_by=principal.user_id))
    audit.record(
        db,
        action="role.granted" if applied else "role.already_present",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=user.tenant_id,
        target_type="user",
        target_id=user_id,
        detail={"role": role.code, "username": user.username},
        ip_address=_ip(request),
    )
    db.commit()
    return {"user_id": user_id, "role": role.code, "applied": applied}


@router.delete("/users/{user_id}/roles/{role_code}", summary="收回用户的角色",
               dependencies=[Depends(require_user_manage)])
def revoke_role(
    user_id: str, role_code: str, request: Request,
    db: DatabaseSession, principal: CurrentPrincipal,
) -> dict[str, Any]:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.is_protected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="固定管理员的角色不可收回")
    link = db.execute(
        select(UserRole)
        .join(Role, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.code == role_code)
    ).scalar_one_or_none()
    applied = link is not None
    if link is not None:
        db.delete(link)
    audit.record(
        db,
        action="role.revoked" if applied else "role.not_present",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=user.tenant_id,
        target_type="user",
        target_id=user_id,
        detail={"role": role_code, "username": user.username},
        ip_address=_ip(request),
    )
    db.commit()
    return {"user_id": user_id, "role": role_code, "applied": applied}


@router.post("/users/{user_id}/permissions", summary="给单个用户加权限或收权限",
             dependencies=[Depends(require_user_manage)])
def grant_permission(
    user_id: str, payload: PermissionGrant, request: Request,
    db: DatabaseSession, principal: CurrentPrincipal,
) -> GrantResultOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if user.is_protected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="固定管理员拥有全部权限，无需也不能单独授权"
        )
    permission = db.execute(
        select(Permission).where(Permission.code == payload.permission_code)
    ).scalar_one_or_none()
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="权限码不存在，请先登记"
        )
    if payload.effect == "allow":
        try:
            assert_can_delegate(principal, payload.permission_code)
        except PermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"该权限不可转授，只能由固定管理员授予：{payload.permission_code}",
            ) from exc

    record_row = db.execute(
        select(UserPermission).where(
            UserPermission.user_id == user_id,
            UserPermission.permission_id == permission.id,
        )
    ).scalar_one_or_none()
    applied = record_row is None or record_row.effect != payload.effect
    if record_row is None:
        db.add(
            UserPermission(
                user_id=user_id,
                permission_id=permission.id,
                effect=payload.effect,
                reason=payload.reason,
                granted_by=principal.user_id,
            )
        )
    else:
        record_row.effect = payload.effect
        record_row.reason = payload.reason
        record_row.granted_by = principal.user_id
        record_row.granted_at = datetime.now().astimezone()
    audit.record(
        db,
        action="permission.granted" if payload.effect == "allow" else "permission.denied",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=user.tenant_id,
        target_type="user",
        target_id=user_id,
        detail={
            "permission": payload.permission_code,
            "effect": payload.effect,
            "username": user.username,
            "reason": payload.reason,
        },
        ip_address=_ip(request),
    )
    db.commit()
    return GrantResultOut(
        user_id=user_id,
        permission_code=payload.permission_code,
        effect=payload.effect,
        applied=applied,
    )


# ---------------- 审计 ----------------
@router.get("/audit-logs", summary="查询审计日志（只读）",
            dependencies=[Depends(require_permission("audit.view"))])
def list_audit_logs(
    db: DatabaseSession,
    action: str | None = Query(default=None, description="按动作过滤，如 user.created"),
    actor_id: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
    ).scalars().all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [AuditLogOut.model_validate(row, from_attributes=True) for row in rows],
    }

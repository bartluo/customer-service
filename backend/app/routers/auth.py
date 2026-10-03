"""认证接口：登录、修改口令、查看当前身份。"""

from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config import get_settings
from app.dependencies import CurrentPrincipal, CurrentUser, DatabaseSession
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    SessionInfoOut,
    UserProfileOut,
)
from app.security.passwords import password_problems, verify_password
from app.security.tokens import create_access_token
from app.services import audit
from app.services.seed import run_seeds

router = APIRouter(prefix="/auth", tags=["认证"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


@router.post("/login", summary="登录并获取访问令牌")
def login(payload: LoginRequest, request: Request, db: DatabaseSession) -> LoginResponse:
    # 首次部署时数据库可能还没有种子数据，这里兜底执行一次，保证"开箱即用"
    run_seeds(db, get_settings())

    from sqlalchemy import select

    from app.models import User

    user = db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none()
    # 用户不存在与口令错误返回同样的信息，避免被用来枚举用户名
    if user is None or not verify_password(payload.password, user.password_hash):
        audit.record(
            db,
            action="auth.login_failed",
            actor_username=payload.username,
            detail={"reason": "用户名或口令错误"},
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或口令错误"
        )
    if not user.is_active:
        audit.record(
            db,
            action="auth.login_blocked",
            actor_id=user.id,
            actor_username=user.username,
            tenant_id=user.tenant_id,
            detail={"reason": "账号已停用"},
            ip_address=_client_ip(request),
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用")

    from app.services.authorization import load_principal

    principal = load_principal(db, user)
    settings = get_settings()
    token = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        is_protected=user.is_protected,
    )
    user.last_login_at = datetime.now(timezone.utc)
    audit.record(
        db,
        action="auth.login",
        actor_id=user.id,
        actor_username=user.username,
        tenant_id=user.tenant_id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        must_change_password=user.must_change_password,
        user=UserProfileOut(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            tenant_id=user.tenant_id,
            is_protected=user.is_protected,
            must_change_password=user.must_change_password,
            roles=list(principal.role_codes),
            permissions=sorted(principal.permissions),
        ),
    )


@router.get("/me", summary="查看当前登录身份与权限")
def whoami(user: CurrentUser, principal: CurrentPrincipal) -> SessionInfoOut:
    return SessionInfoOut(
        user=UserProfileOut(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            tenant_id=user.tenant_id,
            is_protected=user.is_protected,
            must_change_password=user.must_change_password,
            roles=list(principal.role_codes),
            permissions=sorted(principal.permissions),
        ),
        last_login_at=user.last_login_at,
    )


@router.post("/change-password", summary="修改自己的口令")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
) -> dict[str, str]:
    from app.security.passwords import hash_password

    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原口令不正确")
    problems = password_problems(
        payload.new_password, min_length=get_settings().bootstrap_admin_password_min_length
    )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="；".join(problems)
        )
    if payload.old_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新口令不能与原口令相同")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    audit.record(
        db,
        action="auth.password_changed",
        actor_id=user.id,
        actor_username=user.username,
        tenant_id=user.tenant_id,
        ip_address=_client_ip(request),
    )
    db.commit()
    return {"message": "口令已更新"}

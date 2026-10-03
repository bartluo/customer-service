"""FastAPI 依赖：数据库会话、当前登录者、权限校验。"""

from __future__ import annotations
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models import User
from app.security.tokens import TokenError, decode_access_token
from app.services.authorization import Principal, load_principal

bearer_scheme = HTTPBearer(auto_error=False, description="登录后获得的访问令牌")

DatabaseSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    db: DatabaseSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """解析 Bearer 令牌并返回当前用户。

    注意：每次请求都重新查库判断账号是否被停用——令牌没过期但账号被停用时必须立刻拒。
    """

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已失效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不存在或已停用",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 令牌里标记为固定管理员，但数据库里已不是：说明数据被改过，拒绝而不是放行
    if bool(payload.get("protected")) != bool(user.is_protected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证与账号状态不一致，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.tenant_id = user.tenant_id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_principal(request: Request, user: CurrentUser, db: DatabaseSession) -> Principal:
    """当前登录者 + 其有效权限集合。"""

    principal = load_principal(db, user)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_permission(permission_code: str):
    """生成"需要某权限"的依赖，用在路由上：dependencies=[Depends(require_permission("audit.view"))]。"""

    def dependency(principal: CurrentPrincipal) -> Principal:
        if not principal.has(permission_code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"缺少权限：{permission_code}",
            )
        return principal

    return dependency


def require_any_permission(*permission_codes: str):
    """任一项权限满足即可（用于管理后台这类"多个角色各看一部分"的页面）。

    为什么需要它：管理后台的概览页同时服务管理员（管账号、看监控）与
    审核专家（管知识、看缺口）。如果给每个角色各开一个接口，
    前端就要按角色拼页面；接口层放开到"这些人之一都能看"更简单，
    也不会因为漏配一个权限就让某个角色整个后台打不开。
    """

    def dependency(principal: CurrentPrincipal) -> Principal:
        if not any(principal.has(code) for code in permission_codes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="缺少权限：" + " 或 ".join(permission_codes),
            )
        return principal

    return dependency

"""JWT 令牌的签发与校验。

令牌里放什么：只放用户标识与租户标识这类"授权必需"的信息，不放权限列表。
为什么：权限会变（管理员随时给人加权限），写进令牌就等于发出去之后改不了，
必须等令牌过期才生效。权限每次请求都从数据库读，保证"刚授权立刻生效"。

为什么不做刷新令牌：首版令牌有效期 24 小时，且财税系统的授权变更全部可审计。
后续若要接入前端长期登录，再单独加 refresh_token。
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
import uuid

import jwt

from app.config import get_settings

TOKEN_TYPE = "access"


class TokenError(Exception):
    """令牌无效或过期。调用方应返回 401。"""


def create_access_token(*, user_id: str, tenant_id: str | None, is_protected: bool) -> str:
    """签发访问令牌。

    is_protected 表示这是固定管理员：写进令牌后，即使数据库被误改，
    中间件也仍然认它是超级账号（正常情况下数据库有唯一约束兜底，这里是第二道保险）。
    """

    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "tid": tenant_id,
        "protected": is_protected,
        "type": TOKEN_TYPE,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """校验并解析令牌。失败一律抛 TokenError。"""

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("登录凭证无效") from exc
    if payload.get("type") != TOKEN_TYPE:
        raise TokenError("登录凭证无效")
    if not payload.get("sub"):
        raise TokenError("登录凭证无效")
    return payload

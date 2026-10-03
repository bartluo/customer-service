"""审计日志写入。只允许追加，不提供修改与删除。"""

from __future__ import annotations
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    db: Session,
    *,
    action: str,
    actor_id: str | None = None,
    actor_username: str | None = None,
    tenant_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """写入一条审计记录。

    detail 里禁止放口令、令牌、身份证号等明文敏感信息；调用方需自行脱敏。
    本函数不做过滤，是有意的：过滤规则集中在调用处更易审阅。
    """

    entry = AuditLog(
        action=action,
        actor_id=actor_id,
        actor_username=actor_username,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
        ip_address=ip_address,
        user_agent=(user_agent or "")[:300] or None,
    )
    db.add(entry)
    return entry

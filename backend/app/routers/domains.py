"""域包管理接口：查看域包、路由试跑、按租户启停域。

这些接口是"引擎与域包解耦"的对外证明：
加一个新域、启停一个租户的域，都不需要改引擎代码，只改数据。
"""

from __future__ import annotations
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import CurrentPrincipal, DatabaseSession, require_permission
from app.domain.permissions import SYSTEM_CONFIG
from app.domain_packs import get_registry
from app.domain_packs.router import route
from app.models import Tenant
from app.services import audit

router = APIRouter(prefix="/domains", tags=["域包"])

require_system_config = require_permission(SYSTEM_CONFIG)


class RoutePreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    tenant_id: str | None = Field(default=None, description="不给则按全部启用域试跑")


class TenantDomainsUpdate(BaseModel):
    subscribed_domains: list[str] = Field(min_length=1, description="该租户启用的域")


@router.get("", summary="已加载的域包列表")
def list_domains() -> dict[str, Any]:
    registry = get_registry()
    if not registry.is_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="域包尚未加载"
        )
    return {"count": len(registry.all_packs()), "items": registry.summary()}


@router.get("/active", summary="启用中的域包")
def list_active_domains() -> dict[str, Any]:
    registry = get_registry()
    return {
        "items": [
            {"domain_id": pack.domain_id, "name": pack.manifest.name}
            for pack in registry.active_packs()
        ]
    }


@router.post("/route-preview", summary="域路由试跑（不提问，只看路由结果）")
def preview_route(payload: RoutePreviewRequest, db: DatabaseSession) -> dict[str, Any]:
    """给管理员调参用：看一个问题会被路由到哪个域、为什么。

    不调用 LLM，纯规则，秒回。调完关键词立刻能验证效果。
    """

    registry = get_registry()
    if not registry.is_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="域包尚未加载"
        )

    tenant_domains: list[str] | None = None
    if payload.tenant_id:
        tenant = db.get(Tenant, payload.tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在")
        tenant_domains = list(tenant.subscribed_domains or [])

    result = route(payload.question, registry.all_packs(), tenant_domains=tenant_domains)
    return {
        "question": payload.question,
        "domain_id": result.domain_id,
        "confidence": result.confidence,
        "scores": result.scores,
        "reason": result.reason,
        "needs_clarification": result.needs_clarification,
        "clarification_hint": result.clarification_hint,
    }


@router.put(
    "/tenants/{tenant_id}",
    summary="设置某租户启用的域",
    dependencies=[Depends(require_system_config)],
)
def update_tenant_domains(
    tenant_id: str,
    payload: TenantDomainsUpdate,
    db: DatabaseSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    """启用/停用租户的域。停用后该域的问题会被路由层直接拒绝。"""

    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在")

    registry = get_registry()
    known = {pack.domain_id for pack in registry.all_packs()}
    unknown = [item for item in payload.subscribed_domains if item not in known]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知域：{unknown}；已加载的域为 {sorted(known)}",
        )

    before = list(tenant.subscribed_domains or [])
    tenant.subscribed_domains = list(payload.subscribed_domains)
    audit.record(
        db,
        action="tenant.domains_updated",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=tenant.id,
        target_type="tenant",
        target_id=tenant.id,
        detail={"before": before, "after": tenant.subscribed_domains},
    )
    db.commit()
    return {"tenant_id": tenant.id, "subscribed_domains": tenant.subscribed_domains}


@router.get("/tenants/{tenant_id}", summary="查看某租户启用的域")
def get_tenant_domains(tenant_id: str, db: DatabaseSession) -> dict[str, Any]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="租户不存在")
    registry = get_registry()
    subscribed = list(tenant.subscribed_domains or [])
    return {
        "tenant_id": tenant.id,
        "subscribed_domains": subscribed,
        "active_among_them": [
            pack.domain_id
            for pack in registry.active_packs()
            if pack.domain_id in subscribed
        ],
    }

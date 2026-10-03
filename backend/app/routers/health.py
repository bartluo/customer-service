"""健康检查接口。

设计约定：
  · GET /api/health        始终返回 200，附带各依赖的明细（便于排查）
  · GET /api/health/ready  依赖不全时返回 503（供负载均衡与容器编排使用）
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Response, status

from app import __version__
from app.config import get_settings
from app.services.dependency_check import run_health_checks

router = APIRouter(tags=["健康检查"])


@router.get("/health", summary="健康检查（明细）")
def health() -> dict[str, object]:
    settings = get_settings()
    report = run_health_checks()
    return {
        "app": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planning_feature_enabled": settings.feature_planning_enabled,
        **report.to_dict(),
    }


@router.get("/health/ready", summary="就绪探测（依赖不全返回 503）")
def readiness(response: Response) -> dict[str, object]:
    report = run_health_checks()
    if report.failed_required:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report.to_dict()

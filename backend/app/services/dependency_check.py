"""依赖服务探活。健康检查只做连接性探测，不执行业务逻辑。"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import httpx
import redis
from sqlalchemy import text

from app.config import get_settings
from app.database.session import engine


@dataclass
class ServiceResult:
    """单个依赖的探活结果。"""

    name: str
    ok: bool
    latency_ms: float
    detail: str | None = None
    required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok" if self.ok else "down",
            "latency_ms": round(self.latency_ms, 2),
            "required": self.required,
            "detail": self.detail,
        }


def _timed(name: str, probe: Callable[[], None], required: bool = True) -> ServiceResult:
    started = time.perf_counter()
    try:
        probe()
    except Exception as exc:  # 探活失败不应中断整体健康检查
        return ServiceResult(
            name=name,
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=f"{type(exc).__name__}: {exc}"[:300],
            required=required,
        )
    return ServiceResult(
        name=name,
        ok=True,
        latency_ms=(time.perf_counter() - started) * 1000,
        required=required,
    )


def check_postgres() -> ServiceResult:
    def probe() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    return _timed("postgres", probe)


def check_redis() -> ServiceResult:
    settings = get_settings()

    def probe() -> None:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.health_check_timeout_seconds,
            socket_timeout=settings.health_check_timeout_seconds,
        )
        try:
            if not client.ping():
                raise RuntimeError("ping 返回 False")
        finally:
            client.close()

    return _timed("redis", probe)


def check_qdrant() -> ServiceResult:
    settings = get_settings()

    def probe() -> None:
        response = httpx.get(
            f"{settings.qdrant_url.rstrip('/')}/readyz",
            timeout=settings.health_check_timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")

    return _timed("qdrant", probe)


def check_embedding() -> ServiceResult:
    settings = get_settings()

    def probe() -> None:
        response = httpx.get(
            f"{settings.embedding_url.rstrip('/')}/health",
            timeout=settings.health_check_timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")

    return _timed("embedding", probe)


_CHECKS: tuple[Callable[[], ServiceResult], ...] = (
    check_postgres,
    check_qdrant,
    check_redis,
    check_embedding,
)


@dataclass
class HealthReport:
    """整体健康报告。"""

    services: dict[str, dict[str, object]] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    failed_required: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "ok" if not self.failed_required else "degraded"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "services": self.services,
            "degraded": self.degraded,
            "failed_required": self.failed_required,
        }


def run_health_checks() -> HealthReport:
    """并行执行全部探活。

    并行而非顺序的原因：在依赖不可用时，顺序执行的总耗时是各项超时之和
    （最坏情况十几秒），并行后总耗时约等于最长的一项，健康检查才能真正"快速失败"。
    输出按服务名排序，保证结果稳定可比对。
    """

    results: dict[str, dict[str, object]] = {}
    degraded: list[str] = []
    failed_required: list[str] = []

    with ThreadPoolExecutor(max_workers=len(_CHECKS), thread_name_prefix="health") as pool:
        futures = {pool.submit(check): check.__name__ for check in _CHECKS}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:  # 兜底：探活函数本身抛出未捕获异常时不让整体失败
                name = futures[future].removeprefix("check_")
                results[name] = {
                    "status": "down",
                    "latency_ms": 0.0,
                    "required": True,
                    "detail": f"{type(exc).__name__}: {exc}"[:300],
                }
                degraded.append(name)
                failed_required.append(name)
                continue

            results[result.name] = result.to_dict()
            if not result.ok:
                degraded.append(result.name)
                if result.required:
                    failed_required.append(result.name)

    report = HealthReport()
    report.services = {name: results[name] for name in sorted(results)}
    report.degraded = sorted(degraded)
    report.failed_required = sorted(failed_required)
    return report

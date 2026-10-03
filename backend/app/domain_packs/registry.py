"""域包注册中心：全局单例，持有已加载的域包并提供查询。

为什么用单例：域包在启动时加载一次，之后每次提问都要查"有哪些域"。
反复读磁盘会拖慢每次问答，也没必要。
"""

from __future__ import annotations
import logging
import threading
from pathlib import Path

from app.domain_packs.loader import DomainPack, load_all_packs

logger = logging.getLogger(__name__)


class DomainRegistry:
    """已加载域包的容器。线程安全：加载用锁，读取不加锁（加载完成后不再变）。"""

    def __init__(self) -> None:
        self._packs: dict[str, DomainPack] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def load(self, domains_root: Path, *, force: bool = False) -> int:
        """加载所有域包。返回域包数量。默认幂等：已加载过就直接返回。"""

        with self._lock:
            if self._loaded and not force:
                return len(self._packs)
            self._packs = load_all_packs(domains_root)
            self._loaded = True
            active = [p.domain_id for p in self._packs.values() if p.is_active]
            logger.info(
                "已加载 %d 个域包（启用中：%s）",
                len(self._packs),
                "、".join(active) or "无",
            )
            return len(self._packs)

    def get(self, domain_id: str) -> DomainPack | None:
        return self._packs.get(domain_id)

    def all_packs(self) -> list[DomainPack]:
        return list(self._packs.values())

    def active_packs(self) -> list[DomainPack]:
        return [pack for pack in self._packs.values() if pack.is_active]

    def is_loaded(self) -> bool:
        return self._loaded

    def summary(self) -> list[dict[str, object]]:
        """给管理接口用的域包概览。"""

        return [
            {
                "domain_id": pack.domain_id,
                "name": pack.manifest.name,
                "version": pack.manifest.version,
                "status": pack.manifest.status,
                "description": pack.manifest.description,
                "ready_components": list(pack.manifest.ready_components()),
                "pending_components": list(pack.manifest.missing_components()),
                "features": pack.manifest.features,
            }
            for pack in self._packs.values()
        ]


_registry = DomainRegistry()


def get_registry() -> DomainRegistry:
    """全局注册中心。"""

    return _registry

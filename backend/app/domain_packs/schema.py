"""域包 manifest 的结构定义。

用 dataclass 而不是直接读 dict：字段名写错、类型写错在加载时就报出来，
不用等到运行时才炸。
"""

from __future__ import annotations
from dataclasses import dataclass, field

# 九个组件（技术方案 3.2）。全部必填，值可以是文件路径或 pending。
COMPONENT_NAMES: tuple[str, ...] = (
    "ontology",
    "extractor",
    "retrieval",
    "reasoner",
    "verifier",
    "templates",
    "eval_cases",
    "glossary",
    "risk_rules",
)

VALID_STATUSES: frozenset[str] = frozenset({"active", "planned", "disabled"})


@dataclass(frozen=True)
class ComponentSpec:
    """单个组件的声明。status=ready 时 path 必须存在。"""

    status: str  # pending | ready
    path: str | None = None
    note: str = ""

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class RoutingHints:
    """域路由提示词。给路由模型看的，不是硬编码。"""

    keywords: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainPackManifest:
    """一个域包的清单。"""

    domain_id: str
    name: str
    version: str
    status: str
    description: str
    # 本域用到的角色码（对应 11.1 的 admin / reviewer / client）
    roles: tuple[str, ...]
    components: dict[str, ComponentSpec]
    routing: RoutingHints = field(default_factory=RoutingHints)
    features: dict[str, bool] = field(default_factory=dict)
    scope: dict[str, object] = field(default_factory=dict)
    # 财税域的税种分批；其他域留空
    tax_batches: dict[str, list[str]] = field(default_factory=dict)

    def ready_components(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self.components.items() if spec.is_ready)

    def missing_components(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self.components.items() if not spec.is_ready)

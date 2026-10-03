"""域包（Domain Pack）框架。

域包 = 引擎的"插件"。引擎负责通用能力（检索调度、验证调度、模板渲染、评测），
每个领域把自己的知识结构、处理规则、校验规则、输出格式打包成一个目录。

核心约束（技术方案 3.1 / 验收门 G2）：新增一个域不需要改引擎代码。
"""

from app.domain_packs.loader import (
    DomainPack,
    DomainPackError,
    ManifestValidationError,
    load_all_packs,
)
from app.domain_packs.registry import DomainRegistry, get_registry
from app.domain_packs.router import RouteResult, route

__all__ = [
    "DomainPack",
    "DomainRegistry",
    "DomainPackError",
    "ManifestValidationError",
    "RouteResult",
    "get_registry",
    "load_all_packs",
    "route",
]

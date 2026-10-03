"""ORM 模型统一导出。

为什么集中导出：Alembic 的 autogenerate 与 Base.metadata 依赖"所有模型已被导入"。
只要业务代码 import 过这里的模块，新表就能被自动发现。
"""

from app.models.account import (
    AuditLog,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserPermission,
    UserRole,
)
from app.models.knowledge import (
    Regulation,
    RegulationArticle,
    RegulationArticleVersion,
    RegulationRelation,
)
from app.models.verification import VerificationRecord
from app.models.planning import PlanningReview, PlanningTechnique
from app.models.evolution import EvolutionEvent, KnowledgeGap
from app.models.eval import (
    CASE_INSUFFICIENT,
    CASE_REDLINE_TRAP,
    CASE_REPEALED_TRAP,
    CASE_STANDARD,
    CASE_TYPES,
    EvalCase,
    EvalRun,
    GateDecision,
)

__all__ = [
    "AuditLog",
    "Permission",
    "EvolutionEvent",
    "CASE_INSUFFICIENT",
    "CASE_REDLINE_TRAP",
    "CASE_REPEALED_TRAP",
    "CASE_STANDARD",
    "CASE_TYPES",
    "EvalCase",
    "EvalRun",
    "GateDecision",
    "KnowledgeGap",
    "PlanningTechnique",
    "PlanningReview",
    "Regulation",
    "RegulationArticle",
    "RegulationArticleVersion",
    "RegulationRelation",
    "Role",
    "RolePermission",
    "Tenant",
    "User",
    "UserPermission",
    "UserRole",
    "VerificationRecord",
]

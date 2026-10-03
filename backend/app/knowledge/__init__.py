"""财税知识结构化：枚举、文号识别、条文切分、时效抽取。

本包只放纯逻辑（不依赖数据库与网络），因此可以直接单元测试。
数据库写入与导入流水线在 app/services/ingest 下。
"""

from app.knowledge.ontology import (
    CITABLE_EFFECT_STATUSES,
    CONDITIONAL_EFFECT_STATUSES,
    EFFECT_STATUSES,
    GRANULARITY_LEVELS,
    HIERARCHY_LEVELS,
    NON_CITABLE_EFFECT_STATUSES,
    REFERENCE_UNITS,
    REGION_SCOPES,
    RELATION_TYPES,
    REVIEW_STATES,
    is_citable,
)
from app.knowledge.parser import ParsedBlock, ParsedRegulation, parse_regulation, split_articles
from app.knowledge.relations import ExtractedRelation, extract_relations
from app.knowledge.units import KnowledgeUnit, extract_knowledge_units, score_unit_usability
from app.knowledge.versioning import (
    AmendmentResult,
    apply_amendment,
    apply_repeal,
    compute_effect_status,
    detect_repeal_targets,
    is_citable_version,
    status_as_of,
    utc,
)

__all__ = [
    "CITABLE_EFFECT_STATUSES",
    "CONDITIONAL_EFFECT_STATUSES",
    "EFFECT_STATUSES",
    "GRANULARITY_LEVELS",
    "HIERARCHY_LEVELS",
    "NON_CITABLE_EFFECT_STATUSES",
    "REFERENCE_UNITS",
    "REGION_SCOPES",
    "RELATION_TYPES",
    "REVIEW_STATES",
    "is_citable",
    "ParsedBlock",
    "ParsedRegulation",
    "parse_regulation",
    "split_articles",
    "AmendmentResult",
    "apply_amendment",
    "apply_repeal",
    "compute_effect_status",
    "detect_repeal_targets",
    "is_citable_version",
    "status_as_of",
    "utc",
    "ExtractedRelation",
    "extract_relations",
    "KnowledgeUnit",
    "extract_knowledge_units",
    "score_unit_usability",
]

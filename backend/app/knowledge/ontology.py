"""财税域枚举：位阶、效力状态、粒度、关系类型、地域。

为什么放在代码里而不只放在 yaml：效力状态是"废止条文绝不能出现在答案里"
这条硬规则（ontology.yaml validations.no_cite_repealed）的直接输入。
只放在 yaml 里，某个文件忘了读就会静默放行；放在 Python 里能被测试直接断言。

ADR-0006：枚举以技术方案第 4.1 节为唯一权威，与
domains/finance_tax/ontology.yaml 保持一致（由测试断言防漂移）。
"""

from __future__ import annotations

# ---------- 效力位阶（顺序即位阶，数字越小位阶越高） ----------
HIERARCHY_LEVELS: tuple[str, ...] = (
    "law",
    "administrative_regulation",
    "departmental_rule",
    "normative_document",
    "local_normative",
    "normative_reply",
)

# ---------- 效力状态（ADR-0006 定稿） ----------
EFFECT_STATUSES: tuple[str, ...] = (
    "not_yet_effective",
    "effective",
    "partially_repealed",
    "repealed",
    "superseded",
    "draft",
)

# 禁止引用：明文废止、被整体替代、未正式发布的草案
NON_CITABLE_EFFECT_STATUSES: tuple[str, ...] = ("repealed", "superseded", "draft")
# 条件可引用：已公布未到生效日，只能提示"自X日起施行"
CONDITIONAL_EFFECT_STATUSES: tuple[str, ...] = ("not_yet_effective",)
# 可直接作为答案依据
CITABLE_EFFECT_STATUSES: tuple[str, ...] = ("effective", "partially_repealed")

# ---------- 条文粒度（技术方案 4.1，顺序即层级顺序） ----------
GRANULARITY_LEVELS: tuple[str, ...] = (
    "chapter",
    "section",
    "article",
    "paragraph",
    "item",
    "subitem",
)

# 引用单位：条、款、项（目不单独引用）
REFERENCE_UNITS: tuple[str, ...] = ("article", "paragraph", "item")

# ---------- 法规关系类型（技术方案 4.1 relation_types） ----------
RELATION_TYPES: tuple[str, ...] = (
    "based_on",
    "amends",
    "amended_by",
    "repeals",
    "repealed_by",
    "references",
    "referenced_by",
    "excepts",
    "excepted_by",
)

# ---------- 地域维度 ----------
REGION_SCOPES: tuple[str, ...] = ("national", "provincial", "municipal", "district")

# ---------- 导入审核状态（导入流水线内部状态，非效力状态） ----------
REVIEW_STATES: tuple[str, ...] = ("pending_review", "published", "rejected")


def is_citable(effect_status: str) -> bool:
    """能否作为答案依据。检索硬过滤与答案生成都走这一个判断。"""

    return effect_status in CITABLE_EFFECT_STATUSES


def is_valid_at(effect_status: str, as_of: str) -> bool:
    """时点可引用判断。as_of 为 "YYYY-MM-DD" 字符串，避免各处重复解析日期。"""

    if not is_citable(effect_status):
        return False
    return True

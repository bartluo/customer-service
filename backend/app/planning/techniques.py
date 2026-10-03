"""筹划手法库的加载与匹配（"路径 B：结构与安排型"）。

匹配逻辑很直白：拿企业画像去逐条核对手法库里的 `conditions`。
关键在于**三态而不是两态**：
  匹配 / 不匹配 / **信息不足**

财税筹划里"信息不足"太常见了（不知道从业人数、不知道能不能改合同）。
把"信息不足"当成"不匹配"，用户会以为这条路径走不通；
当成"匹配"，系统又在没有依据的情况下给了方案。
所以第三种状态必须单独存在，并且明确告诉用户缺什么。
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from decimal import Decimal

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import domain_file
from app.models.planning import PlanningTechnique
from app.planning.profile import CompanyProfile

_DEFAULT_FILE = domain_file("finance_tax", "planning_techniques.yaml")

# 判断一段依据文本是"文号"还是"法规名 + 条款号"。
# 手法库里两种写法都有：
#   财政部 税务总局公告2023年第19号        → 文号
#   中华人民共和国增值税法 第二十三条       → 法规名 + 条款
_DOC_NUMBER_HINT = re.compile(
    r"〔\d{4}〕|\[\d{4}\]|公告\s*\d{4}\s*年第\s*\d+\s*号|令第[\d一二三四五六七八九十]+号"
)


def parse_citation(text: str) -> dict:
    """把手法库里的依据文本转成验证器能核对的引用结构。

    为什么必须判断类型：验证器有条规定是"给了文号就只按文号核对，
    找不到就是引用无效"。如果把一个法规名当成文号塞进去，
    依据检查会把本来正确的手法判成"依据不存在"。
    """

    cleaned = (text or "").strip()
    if _DOC_NUMBER_HINT.search(cleaned):
        return {
            "document_number": cleaned,
            "regulation_title": None,
            "full_no": None,
            "effect_status": "effective",
        }
    # 法规名本身可能带空格（"财政部 税务总局关于……的公告"），
    # 所以不能按第一个空格切。只有当最后一段以"第"开头时，它才是条款号。
    head, _, tail = cleaned.rpartition(" ")
    if head and tail.startswith("第"):
        title, clause = head, tail
    else:
        title, clause = cleaned, None
    return {
        "document_number": None,
        "regulation_title": title,
        "full_no": clause,
        "effect_status": "effective",
    }


class TechniqueError(ValueError):
    """手法条目本身有问题。"""


@dataclass
class ConditionCheck:
    description: str
    satisfied: bool | None  # None = 信息不足
    detail: str = ""


@dataclass
class TechniqueMatch:
    """一条手法的匹配结果。"""

    technique: PlanningTechnique
    matched: bool | None  # None = 信息不足
    checks: list[ConditionCheck] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [item.description for item in self.checks if item.satisfied is None]

    @property
    def violated(self) -> list[str]:
        return [item.description for item in self.checks if item.satisfied is False]

    def to_dict(self) -> dict:
        return {
            "code": self.technique.code,
            "name": self.technique.name,
            "category": self.technique.category,
            "risk_level": self.technique.risk_level,
            "matched": self.matched,
            "violated": self.violated,
            "missing": self.missing,
            "mechanism": self.technique.mechanism,
            "citations": list(self.technique.citations or []),
            "abuse_boundary": list(self.technique.abuse_boundary or []),
            "rejected_cases": list(self.technique.rejected_cases or []),
            "review_state": self.technique.review_state,
        }


def load_technique_file(path: str | pathlib.Path | None = None) -> list[dict]:
    """读手法素材文件。"""

    file_path = pathlib.Path(path) if path else _DEFAULT_FILE
    if not file_path.exists():
        raise TechniqueError(f"手法文件不存在：{file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    items = data.get("techniques") or []
    if not items:
        raise TechniqueError("手法文件里没有 techniques")
    for item in items:
        for required in ("code", "name", "category", "conditions", "citations", "mechanism",
                         "risk_level", "abuse_boundary"):
            if required not in item:
                raise TechniqueError(f"手法 {item.get('code')} 缺少必填字段：{required}")
        if item["risk_level"] not in {"green", "yellow", "red"}:
            raise TechniqueError(f"手法 {item['code']} 的风险等级不合法：{item['risk_level']}")
    return items


def import_techniques(
    session: Session, path: str | pathlib.Path | None = None, *, review_state: str = "draft"
) -> dict:
    """把手法素材导入库（幂等：按 code 更新）。"""

    created = 0
    updated = 0
    for item in load_technique_file(path):
        existing = session.execute(
            select(PlanningTechnique).where(PlanningTechnique.code == item["code"])
        ).scalars().first()
        fields = {
            "name": item["name"],
            "category": item["category"],
            "conditions": item["conditions"],
            "citations": item["citations"],
            "mechanism": item["mechanism"],
            "risk_level": item["risk_level"],
            "abuse_boundary": item["abuse_boundary"],
            "rejected_cases": item.get("rejected_cases") or [],
            "playbook": item.get("playbook") or {},
            "measure": item.get("measure") or {},
        }
        if existing is None:
            session.add(
                PlanningTechnique(
                    code=item["code"],
                    review_state=review_state,
                    source=item.get("source", "domains/finance_tax/planning_techniques.yaml"),
                    **fields,
                )
            )
            created += 1
        else:
            for key, value in fields.items():
                setattr(existing, key, value)
            updated += 1
    session.commit()
    return {"created": created, "updated": updated}


class TechniqueLibrary:
    """手法库的查询与匹配。"""

    def __init__(self, session: Session, *, include_draft: bool = True) -> None:
        self.session = session
        self.include_draft = include_draft

    def all(self) -> list[PlanningTechnique]:
        statement = select(PlanningTechnique)
        if not self.include_draft:
            statement = statement.where(PlanningTechnique.review_state == "published")
        return list(self.session.execute(statement).scalars().all())

    def match(self, profile: CompanyProfile) -> list[TechniqueMatch]:
        """按画像匹配全部手法。"""

        return [self.match_one(profile, item) for item in self.all()]

    def match_one(self, profile: CompanyProfile, technique: PlanningTechnique) -> TechniqueMatch:
        checks = [
            self._check_condition(profile, condition)
            for condition in (technique.conditions or [])
        ]
        if any(item.satisfied is False for item in checks):
            matched: bool | None = False
        elif any(item.satisfied is None for item in checks):
            matched = None
        else:
            matched = True
        return TechniqueMatch(technique=technique, matched=matched, checks=checks)

    # ------------------------------------------------------------------
    @staticmethod
    def _check_condition(profile: CompanyProfile, condition: dict) -> ConditionCheck:
        description = condition.get("description") or (
            f"{condition.get('field')} {condition.get('op')} {condition.get('value')}"
        )
        field_name = condition.get("field") or ""
        op = (condition.get("op") or "=").lower()
        expected = condition.get("value")

        actual = TechniqueLibrary._resolve(profile, field_name)
        if actual is None:
            if op == "exists":
                return ConditionCheck(description, None, f"画像里没有“{field_name}”这项信息")
            return ConditionCheck(description, None, f"缺“{description}”所需的信息")
        if op == "exists":
            return ConditionCheck(description, bool(actual) is bool(expected))
        if op == "truthy":
            return ConditionCheck(description, bool(actual))
        if op == "in":
            values = expected if isinstance(expected, list) else [expected]
            return ConditionCheck(description, actual in values, f"实际为 {actual}")
        if op in {"<=", ">=", "<", ">"}:
            try:
                left = Decimal(str(actual))
                right = Decimal(str(expected))
            except Exception:  # noqa: BLE001
                return ConditionCheck(description, None, "数值无法比较")
            satisfied = {
                "<=": left <= right,
                ">=": left >= right,
                "<": left < right,
                ">": left > right,
            }[op]
            return ConditionCheck(description, satisfied, f"实际为 {actual}，要求 {op} {expected}")
        if op in {"=", "=="}:
            return ConditionCheck(description, actual == expected, f"实际为 {actual}")
        if op in {"!=", "<>"}:
            return ConditionCheck(description, actual != expected, f"实际为 {actual}")
        return ConditionCheck(description, None, f"不支持的判断符：{op}")

    @staticmethod
    def _resolve(profile: CompanyProfile, field_name: str) -> object:
        """按字段名取画像里的值，支持 flexible.can_change_entity 这种二级路径。"""

        if not field_name:
            return None
        head, _, tail = field_name.partition(".")
        value = getattr(profile, head, None)
        if tail and isinstance(value, dict):
            return value.get(tail)
        return value

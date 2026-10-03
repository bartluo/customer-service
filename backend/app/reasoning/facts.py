"""事实抽取：把用户的口语描述变成结构化税企要素。

设计要点：
  1. **规则驱动、可配置**。词表与正则放在 domains/finance_tax/reasoner.yaml，
     改词表不用改代码，审核专家也能维护。
  2. **每个要素都记录来源**。抽到的每个值都带一句"依据哪段原文"，
     用户质疑时可以指回去，而不是"系统就这么认为的"。
  3. **抽不到就进待补清单，绝不脑补**。这一点写成了代码约束：
     只有明确声明（"我是小规模纳税人"）才算确认；
     像"我这小店"这种只能"推测"的说法，值会填上但标 needs_confirmation=True，
     由调用方决定是不是要追问。
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import yaml

from app.config import domain_file
from app.calculation.money import yuan

_DEFAULT_CONFIG = domain_file("finance_tax", "reasoner.yaml")


@dataclass
class ExtractEvidence:
    """一个要素的来源说明。"""

    field: str  # 要素名，如"纳税人身份"
    value: str
    matched_text: str  # 命中的原文片段
    needs_confirmation: bool = False

    def describe(self) -> str:
        flag = "（需确认）" if self.needs_confirmation else ""
        return f"{self.field}：{self.value}{flag}——依据“{self.matched_text}”"


@dataclass
class ExtractedFacts:
    """一次抽取的结果。"""

    raw_text: str = ""
    taxpayer_type: str | None = None
    taxpayer_type_confirmed: bool = False
    # 用户是不是**在问题里明确说了**主体（"我是小规模纳税人"/"小微企业有什么优惠"）。
    # 为什么要单独记：说的方式和猜的方式，答案的写法不一样——
    # 明确说了就直接按这个主体答；只是从"小卖部"推测出来的，答案里必须标"推测"。
    taxpayer_type_stated: bool = False
    tax_types: list[str] = field(default_factory=list)
    business_type: str | None = None
    amount: Decimal | None = None
    amount_includes_tax: bool | None = None
    amount_period: str | None = None
    input_vat: Decimal | None = None
    period: str | None = None
    # 期间的粒度：month / quarter / year。
    # 小规模纳税人的免税额按月（10万）或按季（30万）判断，
    # 所以"用户说的是月收入还是季度收入"会直接改变结论。
    period_scope: str | None = None
    region: str | None = None
    scale: dict[str, Decimal] = field(default_factory=dict)
    evidence: list[ExtractEvidence] = field(default_factory=list)
    # 抽不到、但会影响结论的要素
    missing: list[str] = field(default_factory=list)
    # 建议向用户追问的问题（一次最多两个，见 reasoner.yaml 的 clarification）
    questions: list[str] = field(default_factory=list)
    # 追问里的"主体那一问"。政策类问题只需要问这一句——
    # 用户问"有哪些税收优惠"时，跟着追问"金额是多少"就属于多问（他还没到算钱那一步）。
    subject_question: str | None = None

    def to_dict(self) -> dict:
        return {
            "taxpayer_type": self.taxpayer_type,
            "taxpayer_type_confirmed": self.taxpayer_type_confirmed,
            "taxpayer_type_stated": self.taxpayer_type_stated,
            "tax_types": list(self.tax_types),
            "business_type": self.business_type,
            "amount": str(self.amount) if self.amount is not None else None,
            "amount_includes_tax": self.amount_includes_tax,
            "amount_period": self.amount_period,
            "input_vat": str(self.input_vat) if self.input_vat is not None else None,
            "period": self.period,
            "period_scope": self.period_scope,
            "region": self.region,
            "scale": {key: str(value) for key, value in self.scale.items()},
            "evidence": [item.describe() for item in self.evidence],
            "missing": list(self.missing),
            "questions": list(self.questions),
            "subject_question": self.subject_question,
        }


class FactExtractor:
    """按配置从口语文本里抽要素。"""

    def __init__(self, config: dict) -> None:
        self.config = config.get("fact_extraction") or {}
        self.clarification = config.get("clarification") or {}

    # ------------------------------------------------------------------
    def extract(self, text: str) -> ExtractedFacts:
        facts = ExtractedFacts(raw_text=text or "")
        if not text:
            self._finalize(facts)
            return facts

        self._extract_taxpayer(facts, text)
        self._extract_amount(facts, text)
        self._extract_period(facts, text)
        self._extract_region(facts, text)
        self._extract_business(facts, text)
        self._extract_scale(facts, text)
        self._extract_tax_types(facts, text)

        self._finalize(facts)
        return facts

    # ------------------------------------------------------------------
    def _extract_taxpayer(self, facts: ExtractedFacts, text: str) -> None:
        for entry in self.config.get("taxpayer_types") or []:
            for pattern in entry.get("patterns") or []:
                match = re.search(pattern, text)
                if match:
                    facts.taxpayer_type = entry["value"]
                    needs_confirm = bool(entry.get("needs_confirm"))
                    facts.taxpayer_type_confirmed = not needs_confirm
                    stated = entry.get("stated_patterns") or entry.get("patterns") or []
                    facts.taxpayer_type_stated = any(
                        re.search(item, match.group(0)) for item in stated
                    )
                    facts.evidence.append(
                        ExtractEvidence(
                            field="纳税人身份",
                            value=entry["value"],
                            matched_text=match.group(0),
                            needs_confirmation=needs_confirm,
                        )
                    )
                    return

    def _extract_amount(self, facts: ExtractedFacts, text: str) -> None:
        spec = self.config.get("amount") or {}
        units = {str(k): Decimal(str(v)) for k, v in (spec.get("units") or {}).items()}
        # 金额：必须有单位（万/亿/千）或"元"字样。
        # 只说数字不认——"3 个月"里的 3 不是金额。
        # 但"103万"这种省略"元"的说法很常见，必须认。
        pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(?:(亿|万|千)\s*元?|元)")
        match = pattern.search(text)
        if not match:
            return
        number = Decimal(match.group(1))
        unit = match.group(2) or ""
        amount = yuan(number * units.get(unit, Decimal("1")))
        facts.amount = amount
        facts.evidence.append(
            ExtractEvidence(field="金额", value=f"{amount} 元", matched_text=match.group(0))
        )

        # 必须先判"不含税"再判"含税"：中文里"不含税"包含"含税"这四个字里的
        # "含税"子串，顺序反了会把"不含税"判成"含税"——金额口径直接判反。
        if any(word in text for word in spec.get("excludes_tax_words") or []):
            facts.amount_includes_tax = False
        elif any(word in text for word in spec.get("includes_tax_words") or []):
            facts.amount_includes_tax = True

        for period_name, words in (spec.get("period_words") or {}).items():
            if any(word in text for word in words):
                facts.amount_period = period_name
                break

        # 进项税额单独抽：它是计算一般计税的必要输入，混在金额里会被当成销售额
        input_pattern = re.compile(r"进项税额?\s*(?:为|是|有)?\s*(\d+(?:\.\d+)?)\s*(?:(亿|万|千)\s*元?|元)")
        input_match = input_pattern.search(text)
        if input_match:
            number = Decimal(input_match.group(1))
            unit = input_match.group(2) or ""
            facts.input_vat = yuan(number * units.get(unit, Decimal("1")))
            facts.evidence.append(
                ExtractEvidence(
                    field="进项税额",
                    value=f"{facts.input_vat} 元",
                    matched_text=input_match.group(0),
                )
            )

    def _extract_period(self, facts: ExtractedFacts, text: str) -> None:
        for entry in self.config.get("periods") or []:
            for pattern in entry.get("patterns") or []:
                match = re.search(pattern, text)
                if match:
                    value = match.group(0) if entry["value"].startswith("指定") else entry["value"]
                    facts.period = value.strip()
                    facts.period_scope = entry.get("scope") or None
                    facts.evidence.append(
                        ExtractEvidence(
                            field="期间", value=facts.period, matched_text=match.group(0)
                        )
                    )
                    return

    def _extract_region(self, facts: ExtractedFacts, text: str) -> None:
        for entry in self.config.get("regions") or []:
            for pattern in entry.get("patterns") or []:
                match = re.search(pattern, text)
                if match:
                    facts.region = entry["value"]
                    facts.evidence.append(
                        ExtractEvidence(
                            field="地区", value=entry["value"], matched_text=match.group(0)
                        )
                    )
                    return

    def _extract_scale(self, facts: ExtractedFacts, text: str) -> None:
        units = {"亿": Decimal("100000000"), "万": Decimal("10000"), "": Decimal("1")}
        for name, patterns in (self.config.get("scale_facts") or {}).items():
            for pattern in patterns:
                match = re.search(pattern, text)
                if not match:
                    continue
                try:
                    number = Decimal(match.group(1))
                except (InvalidOperation, IndexError):
                    continue
                unit = ""
                if match.lastindex and match.lastindex >= 2 and match.group(2):
                    unit = match.group(2)
                value = yuan(number * units.get(unit, Decimal("1")))
                facts.scale[name] = value
                facts.evidence.append(
                    ExtractEvidence(field=name, value=str(value), matched_text=match.group(0))
                )
                break

    def _extract_business(self, facts: ExtractedFacts, text: str) -> None:
        """抽业务类型。值必须能对上计算引擎的税率关键词，否则税率还是选不出来。"""

        for entry in self.config.get("business_types") or []:
            for pattern in entry.get("patterns") or []:
                match = re.search(pattern, text)
                if match:
                    facts.business_type = entry["value"]
                    facts.evidence.append(
                        ExtractEvidence(
                            field="业务类型", value=entry["value"], matched_text=match.group(0)
                        )
                    )
                    return

    def _extract_tax_types(self, facts: ExtractedFacts, text: str) -> None:
        from app.domain.tax_types import detect_tax_types

        facts.tax_types = detect_tax_types(text)

    # ------------------------------------------------------------------
    def _finalize(self, facts: ExtractedFacts) -> None:
        """算缺失清单与追问清单。"""

        if not facts.taxpayer_type:
            facts.missing.append("纳税人身份（一般纳税人 / 小规模纳税人）")
        elif not facts.taxpayer_type_confirmed:
            facts.missing.append(f"纳税人身份的确认（根据描述推测为“{facts.taxpayer_type}”）")
        if not facts.period:
            facts.missing.append("适用期间")
        if not facts.business_type:
            facts.missing.append("业务类型")
        if facts.amount is None:
            facts.missing.append("金额")
        elif facts.amount_includes_tax is None:
            facts.missing.append("金额口径（含税 / 不含税）")

        facts.questions = self._build_questions(facts)

    def _build_questions(self, facts: ExtractedFacts) -> list[str]:
        """把缺失项变成具体问题。一次最多问两个（问多了用户会放弃）。"""

        limit = int(self.clarification.get("max_questions") or 2)
        questions: list[str] = []
        if not facts.taxpayer_type:
            # 先问主体：优惠能不能享受、税怎么算，几乎都取决于它。
            # 只问"一般纳税人还是小规模纳税人"不够——个体工商户 / 小型微利企业
            # 是另一个维度，漏问了会答错方向。
            facts.subject_question = (
                "您的企业类型与纳税人身份分别是什么"
                "（例如：小型微利企业 / 个体工商户 / 有限公司；一般纳税人 / 小规模纳税人）？"
            )
            questions.append(facts.subject_question)
        elif not facts.taxpayer_type_confirmed:
            facts.subject_question = f"您的情况是“{facts.taxpayer_type}”吗？"
            questions.append(facts.subject_question)
        if facts.amount is not None and facts.amount_includes_tax is None:
            questions.append(f"您说的 {facts.amount} 元是含税金额还是不含税金额？")
        elif facts.amount is None:
            questions.append("涉及的金额是多少（含税还是不含税）？")
        if not facts.period:
            questions.append("这笔业务发生在什么期间（例如 2026 年 8 月）？")
        if not facts.business_type:
            questions.append("具体是什么业务（例如销售货物 / 提供服务）？")
        return questions[:limit]


def load_extractor_config(path: str | pathlib.Path | None = None) -> dict:
    file_path = pathlib.Path(path) if path else _DEFAULT_CONFIG
    if not file_path.exists():
        raise FileNotFoundError(f"推理配置不存在：{file_path}")
    return yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}


_extractor: FactExtractor | None = None


def get_extractor(path: str | pathlib.Path | None = None) -> FactExtractor:
    global _extractor
    if _extractor is None:
        _extractor = FactExtractor(load_extractor_config(path))
    return _extractor

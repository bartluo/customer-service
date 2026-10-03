"""适用性判定：对每个候选政策给出"适用 / 不适用 / 需确认"。

技术方案 4.4 第 3 步要求"逐条检查适用条件"，并特别强调：
**不适用清单也要给**——用户问"我这种情况能不能享受 X 优惠"，
只回答"不能"没有价值；说明"因为你的行业不属于 Y 目录，所以不适用"才叫专业。

做法：从条文正文里抽出**可判定的条件**（主体、金额门槛、人数门槛、资产门槛、时点），
拿用户事实逐条比对，得出三种结论之一：
  · 适用     —— 条件都满足
  · 不适用   —— 有一条明确不满足，且说明是哪一条
  · 需确认   —— 条件存在但用户信息不足，明确指出**缺哪一项**

为什么不交给模型判：
  这一步的输入是"条款原文 + 用户要素"，比对逻辑是确定的（数值比大小、主体是否相等）。
  交给模型只会引入不可解释、不可复现的差异。模型适合做的是"条款语义理解"，
  而那部分已经由检索层（语义召回）承担了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from app.reasoning.facts import ExtractedFacts


class Verdict(str, Enum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    NEED_CONFIRM = "need_confirm"


@dataclass
class ConditionResult:
    """一条适用条件的判定结果。"""

    name: str
    passed: bool | None  # None = 信息不足，判不了
    reason: str
    evidence: str = ""  # 条件出自条文里的哪句话


@dataclass
class PolicyVerdict:
    """对一条候选政策的判定。"""

    title: str
    citation: str
    verdict: Verdict
    conditions: list[ConditionResult] = field(default_factory=list)
    # 条文原文。判定的输入本来就带着它，留着是为了让答案能直接引用
    # "这条政策说了什么"——只给文号+条款号的答案对用户没有价值，
    # 用户要的是结论，文号只是出处。
    content: str = ""

    @property
    def reason(self) -> str:
        if not self.conditions:
            return "未发现可判定的适用条件"
        failed = [c for c in self.conditions if c.passed is False]
        unknown = [c for c in self.conditions if c.passed is None]
        if failed:
            return "；".join(c.reason for c in failed)
        if unknown:
            return "；".join(c.reason for c in unknown)
        return "适用条件检查通过：" + "；".join(c.reason for c in self.conditions)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "citation": self.citation,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "content": self.content,
            "conditions": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "reason": c.reason,
                    "evidence": c.evidence,
                }
                for c in self.conditions
            ],
        }


@dataclass
class ApplicabilityReport:
    """一次判定的完整结果：三类清单。"""

    applicable: list[PolicyVerdict] = field(default_factory=list)
    not_applicable: list[PolicyVerdict] = field(default_factory=list)
    need_confirm: list[PolicyVerdict] = field(default_factory=list)

    def add(self, item: PolicyVerdict) -> None:
        target = {
            Verdict.APPLICABLE: self.applicable,
            Verdict.NOT_APPLICABLE: self.not_applicable,
            Verdict.NEED_CONFIRM: self.need_confirm,
        }[item.verdict]
        target.append(item)

    def to_dict(self) -> dict:
        return {
            "applicable": [item.to_dict() for item in self.applicable],
            "not_applicable": [item.to_dict() for item in self.not_applicable],
            "need_confirm": [item.to_dict() for item in self.need_confirm],
        }

    def explain(self) -> str:
        lines: list[str] = []
        for label, items in (
            ("适用", self.applicable),
            ("不适用", self.not_applicable),
            ("需确认", self.need_confirm),
        ):
            lines.append(f"【{label}】{len(items)} 条")
            for item in items:
                lines.append(f"  · {item.citation or item.title}")
                lines.append(f"      {item.reason}")
        return "\n".join(lines)


# 主体条件的词表：条文里出现这些词，就说明该条款对主体有要求
_SUBJECT_WORDS = ("小规模纳税人", "一般纳税人", "个体工商户", "小型微利企业", "个人")

# 主体词属于哪个维度。**这一层不能省**：
#   "小规模纳税人 / 一般纳税人" 是增值税上的身份，
#   "个体工商户 / 小型微利企业 / 企业" 是经营主体类型，
# 两者可以同时成立（一个个体户既是个体工商户、也可能是小规模纳税人）。
# 跨维度直接比大小会得出"您是小规模纳税人，所以小型微利企业政策不适用"这种错结论——
# 正确做法是：不在同一维度 → 判"需确认"，问清楚那一维度。
_SUBJECT_DIMENSION = {
    "小规模纳税人": "增值税纳税人身份",
    "一般纳税人": "增值税纳税人身份",
    "个体工商户": "经营主体类型",
    "小型微利企业": "经营主体类型",
    "企业": "经营主体类型",
    "个人": "经营主体类型",
}

# 金额门槛：不超过 300 万元 / 未超过 10 万元 / 30 万元以下
_THRESHOLD_PATTERNS = (
    (re.compile(r"不超过\s*(\d+(?:\.\d+)?)\s*(亿|万)?元"), "上限"),
    (re.compile(r"未超过\s*(\d+(?:\.\d+)?)\s*(亿|万)?元"), "上限"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(亿|万)?元以下"), "上限"),
)

_COUNT_PATTERNS = {
    "从业人数": re.compile(r"从业人数\s*不超过\s*(\d+)\s*人|(\d+)\s*人以下"),
    "资产总额": re.compile(r"资产总额\s*不超过\s*(\d+(?:\.\d+)?)\s*(亿|万)?元"),
}

_UNITS = {"亿": Decimal("100000000"), "万": Decimal("10000"), "": Decimal("1")}

# 业务类型的正式名称。用于判断"这条规定讲的是不是用户问的这类业务"。
# 只在条文里出现正式名称时才算命中——用户口语（"卖东西"）不会出现在法条里。
_BUSINESS_WORDS = (
    "出口货物",
    "跨境销售",
    "销售货物",
    "加工修理修配",
    "交通运输",
    "建筑服务",
    "不动产租赁",
    "有形动产租赁",
    "现代服务",
    "生活服务",
    "销售不动产",
    "转让无形资产",
)


class ApplicabilityJudge:
    """逐条检查适用条件。"""

    def judge(self, facts: ExtractedFacts, candidates: list[dict]) -> ApplicabilityReport:
        """candidates 每项形如 {title, citation, content}。"""

        report = ApplicabilityReport()
        for candidate in candidates:
            report.add(self.judge_one(facts, candidate))
        # 顺序固定，便于阅读与测试：适用 → 需确认 → 不适用
        return report

    def judge_one(self, facts: ExtractedFacts, candidate: dict) -> PolicyVerdict:
        content = (candidate.get("content") or "").strip()
        verdict = PolicyVerdict(
            title=candidate.get("title", ""),
            citation=candidate.get("citation", ""),
            verdict=Verdict.APPLICABLE,
            content=content,
        )

        verdict.conditions.extend(self._check_subject(facts, content))
        verdict.conditions.extend(self._check_business(facts, content))
        verdict.conditions.extend(self._check_thresholds(facts, content))
        verdict.conditions.extend(self._check_tax_type(facts, candidate))

        failed = [c for c in verdict.conditions if c.passed is False]
        unknown = [c for c in verdict.conditions if c.passed is None]
        if failed:
            verdict.verdict = Verdict.NOT_APPLICABLE
        elif unknown:
            verdict.verdict = Verdict.NEED_CONFIRM
        elif not verdict.conditions:
            # 一条可判定的条件都没抽到，我们就**不能说它适用**。
            # 默认判"适用"等于替用户断言了一个我们没验证过的结论——
            # 这正是"宁可说不知道，也不猜"原则的落点。
            verdict.verdict = Verdict.NEED_CONFIRM
            verdict.conditions.append(
                ConditionResult(
                    name="适用条件",
                    passed=None,
                    reason="该条款未包含可自动判定的适用条件，需人工核对是否适用于您的情况",
                )
            )
        else:
            verdict.verdict = Verdict.APPLICABLE
        return verdict

    # ------------------------------------------------------------------
    def _check_business(self, facts: ExtractedFacts, content: str) -> list[ConditionResult]:
        """业务类型条件：条文讲的是不是用户问的那类业务。

        为什么要单独判这一条：主体对了不代表业务对口。
        用户问"国内卖货怎么交税"，而条文讲的是出口退税，
        主体检查（都是小规模纳税人）会通过，把一条完全不相干的规定
        列进"可以适用"——这是最容易被当成"系统乱推荐"的那种错。
        """

        mentioned = [word for word in _BUSINESS_WORDS if word in content]
        if not mentioned:
            return []
        if not facts.business_type:
            return [
                ConditionResult(
                    name="业务类型",
                    passed=None,
                    reason=f"该条款针对“{'、'.join(mentioned)}”，您的业务类型尚未说明",
                    evidence=self._find_sentence(content, mentioned[0]),
                )
            ]
        if any(word in facts.business_type for word in mentioned):
            return [
                ConditionResult(
                    name="业务类型",
                    passed=True,
                    reason=f"该条款针对“{'、'.join(mentioned)}”，与您的业务一致",
                    evidence=self._find_sentence(content, mentioned[0]),
                )
            ]
        return [
            ConditionResult(
                name="业务类型",
                passed=False,
                reason=(
                    f"该条款针对“{'、'.join(mentioned)}”，"
                    f"与您问的“{facts.business_type}”不是同一类业务"
                ),
                evidence=self._find_sentence(content, mentioned[0]),
            )
        ]

    def _check_subject(self, facts: ExtractedFacts, content: str) -> list[ConditionResult]:
        """主体条件：条文点名了某类主体，用户必须属于该类。"""

        mentioned = [word for word in _SUBJECT_WORDS if word in content]
        if not mentioned:
            return []
        if not facts.taxpayer_type:
            return [
                ConditionResult(
                    name="适用主体",
                    passed=None,
                    reason=f"该条款限定适用主体为“{'、'.join(mentioned)}”，您的情况尚未确认",
                    evidence=self._find_sentence(content, mentioned[0]),
                )
            ]

        user_dimension = _SUBJECT_DIMENSION.get(facts.taxpayer_type)
        same_dimension = [
            word for word in mentioned if _SUBJECT_DIMENSION.get(word) == user_dimension
        ]
        if not same_dimension:
            other = [word for word in mentioned if word not in same_dimension]
            return [
                ConditionResult(
                    name="适用主体",
                    passed=None,
                    reason=(
                        f"该条款限定适用主体为“{'、'.join(mentioned)}”，"
                        f"属于您尚未说明的维度（您已说明的是“{facts.taxpayer_type}”）；"
                        f"例如您是否属于{other[0] if other else mentioned[0]}需要确认"
                    ),
                    evidence=self._find_sentence(content, mentioned[0]),
                )
            ]
        if any(word in facts.taxpayer_type for word in same_dimension):
            caveat = "" if facts.taxpayer_type_confirmed else "（该身份是根据您的描述推测的，尚未确认）"
            return [
                ConditionResult(
                    name="适用主体",
                    passed=True,
                    reason=(
                        f"您的身份“{facts.taxpayer_type}”符合条款限定"
                        f"（{'、'.join(same_dimension)}）{caveat}"
                    ),
                    evidence=self._find_sentence(content, mentioned[0]),
                )
            ]
        return [
            ConditionResult(
                name="适用主体",
                passed=False,
                reason=(
                    f"该条款限定适用主体为“{'、'.join(same_dimension)}”，"
                    f"您的情况是“{facts.taxpayer_type}”"
                ),
                evidence=self._find_sentence(content, mentioned[0]),
            )
        ]

    def _check_thresholds(self, facts: ExtractedFacts, content: str) -> list[ConditionResult]:
        """金额/人数/资产门槛：把条文里的数值和用户提供的数值比大小。

        拿不到用户数值时判"需确认"，并明确指出缺哪一项——
        这正是"不适用清单"之外，"待确认清单"的价值所在。
        """

        results: list[ConditionResult] = []
        for pattern, kind in _THRESHOLD_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            limit = Decimal(match.group(1)) * _UNITS.get(match.group(2) or "", Decimal("1"))
            sentence = self._find_sentence(content, match.group(0))
            value = self._amount_for(facts, sentence)
            if value is None:
                results.append(
                    ConditionResult(
                        name="金额门槛",
                        passed=None,
                        reason=f"条款要求{match.group(0)}，您未提供可比的金额或规模数据",
                        evidence=sentence,
                    )
                )
            elif kind == "上限" and value <= limit:
                results.append(
                    ConditionResult(
                        name="金额门槛",
                        passed=True,
                        reason=f"您的情况（{value} 元）未超过条款上限（{match.group(0)}）",
                        evidence=sentence,
                    )
                )
            else:
                results.append(
                    ConditionResult(
                        name="金额门槛",
                        passed=False,
                        reason=f"您的情况（{value} 元）超过条款上限（{match.group(0)}）",
                        evidence=sentence,
                    )
                )
            break  # 一条条文里只取第一个门槛，避免重复判同一件事

        for name, pattern in _COUNT_PATTERNS.items():
            match = pattern.search(content)
            if not match:
                continue
            raw = match.group(1) or match.group(2)
            unit = ""
            if name == "资产总额" and match.lastindex and match.lastindex >= 3:
                unit = match.group(3) or ""
            limit = Decimal(raw) * _UNITS.get(unit, Decimal("1"))
            sentence = self._find_sentence(content, match.group(0))
            value = facts.scale.get(name)
            if value is None:
                results.append(
                    ConditionResult(
                        name=name,
                        passed=None,
                        reason=f"条款对{name}有要求（{match.group(0)}），您未提供该项数据",
                        evidence=sentence,
                    )
                )
            elif value <= limit:
                results.append(
                    ConditionResult(
                        name=name,
                        passed=True,
                        reason=f"您的{name}（{value}）符合条款要求（{match.group(0)}）",
                        evidence=sentence,
                    )
                )
            else:
                results.append(
                    ConditionResult(
                        name=name,
                        passed=False,
                        reason=f"您的{name}（{value}）超过条款要求（{match.group(0)}）",
                        evidence=sentence,
                    )
                )
        return results

    def _check_tax_type(self, facts: ExtractedFacts, candidate: dict) -> list[ConditionResult]:
        """税种条件：用户问的税种和政策所属税种要能对上。"""

        policy_tax = candidate.get("tax_types") or []
        if not policy_tax or not facts.tax_types:
            return []
        if set(policy_tax) & set(facts.tax_types):
            return [
                ConditionResult(
                    name="税种",
                    passed=True,
                    reason=f"该政策属于“{'、'.join(policy_tax)}”，与您问的税种一致",
                )
            ]
        return [
            ConditionResult(
                name="税种",
                passed=False,
                reason=(
                    f"该政策针对“{'、'.join(policy_tax)}”，"
                    f"与您问的“{'、'.join(facts.tax_types)}”不是同一税种"
                ),
            )
        ]

    @staticmethod
    def _amount_for(facts: ExtractedFacts, sentence: str) -> Decimal | None:
        """决定拿哪个数去比：条文说的是"应纳税所得额"就用规模数据，否则用金额。"""

        for name, value in facts.scale.items():
            if name in sentence:
                return value
        if "销售额" in sentence or "收入" in sentence:
            return facts.amount
        # 条文没点名具体指标时，优先用规模数据，其次用金额
        if facts.scale:
            return next(iter(facts.scale.values()))
        return facts.amount

    @staticmethod
    def _find_sentence(content: str, needle: str) -> str:
        """取出包含关键词的那句话，作为条件依据留痕。"""

        if not content:
            return ""
        for sentence in re.split(r"[。；\n]", content):
            if needle and needle in sentence:
                return sentence.strip()[:120]
        return content[:120]

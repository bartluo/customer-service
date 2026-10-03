"""问答主链路：把前面几步串成一次完整回答。

链路（对应技术方案 4.4 的推理五步）：
    问题 → ① 事实结构化 → ② 候选政策召回 → ③ 适用性判定
                                  ↓
                         ④ 计算编排 → ⑤ 按模板组装答案

三条贯穿原则：
  · **要素不全就问，不硬答**。缺关键要素时改走追问模板，
    照样是按模板输出，而不是给一个残缺的答案。
  · **没有依据就拒答**。检索不到可用条款时按 reasoner.yaml 的
    refusal_rules 拒绝，并说明原因。
  · **数字一律来自计算引擎**。这里不做任何算术。
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.calculation import get_engine
from app.calculation.engine import SurchargeInput, VatInput
from app.calculation.money import money_text
from app.reasoning.applicability import ApplicabilityJudge, ApplicabilityReport, Verdict
from app.reasoning.facts import ExtractedFacts, get_extractor
from app.reasoning.templates import AnswerTemplate, RenderedAnswer, load_templates, render
from app.retrieval.searcher import Citation, KnowledgeSearcher, SearchRequest

# 意图：与技术方案 4.4 及 reasoner.yaml 的 intent_types 对齐
INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "calculation": ("怎么算", "交多少", "多少钱", "税额", "计算", "税负"),
    "how_to": ("怎么办", "如何办理", "流程", "材料", "怎么申请", "怎么申报", "手续"),
    # "被税务机关查了"这种说法很常见，但字面不含"被查"，所以补上"税务机关查/税务局查"
    "inspection": ("被查", "稽查", "检查", "查账", "税务机关查", "税务局查", "税务检查"),
    "dispute": ("争议", "复议", "处罚", "罚款", "滞纳金"),
    "planning": ("筹划", "怎么省税", "少交税", "税务规划"),
    "policy_query": ("能不能", "可以吗", "是否", "有没有优惠", "适用", "政策"),
}


def classify_intent(question: str) -> str:
    """判问题类型。先看有没有计算/办理这类强意图，都没有就当政策查询。"""

    text = question or ""
    for intent in ("inspection", "dispute", "planning", "calculation", "how_to"):
        if any(keyword in text for keyword in INTENT_KEYWORDS[intent]):
            return intent
    return "policy_query"


# 这些问题"答案取决于您是谁"：优惠按主体分档、能不能享受要看类型、算税要看身份。
# 问题里没说主体时，先问一句比直接答一个可能不适用他的结论要好。
# （"增值税税率是多少"这类不在此列——它问的是条文本身，谁问答案都一样。）
SUBJECT_DEPENDENT_KEYWORDS = (
    "优惠",
    "减免",
    "免征",
    "减半",
    "能不能",
    "可不可以",
    "可以吗",
    "能否",
    "适用",
    "怎么交",
    "怎么算",
    "交多少",
    "要交多少",
    "税负",
)


def _stated_subject(facts: ExtractedFacts) -> str | None:
    """用户在问题里明确说了的主体；没说（或只是从线索推测）返回 None。"""

    return facts.taxpayer_type if facts.taxpayer_type_stated else None


# 条文正文短于这个长度，就认为它"只是个标题"（如《增值税法》第十条的正文只有"增值税税率:"）
MIN_POINT_CHARS = 15

# 判断一句条文"是不是在讲能享受什么"。
# 为什么要判：条文分两类——一类写实质内容（"减按25%计算应纳税所得额"），
# 一类只写怎么办手续（"通过填写纳税申报表即可享受"）。
# 用户问"有什么优惠"要的是前者。检索按相关度排序，经常把后者排在前面。
#
# 打分分档，不搞"关键词各加一分"：那样程序性条款靠"减免""标准""条件"
# 这些虚词也能攒到高分，把真正写着比例的条款压下去（实测踩过）。
# 中文字符串没有词边界，"减免税额"里就含"免税"——
# 直接按子串匹配会把"自动计算减免税额"这句程序性条款误判成免税优惠。
# 所以对"免税"加否定前视：前面是"减"的不算。
STRONG_BENEFIT_RE = re.compile(
    r"(?<!减)免征|(?<!减)免税|减半征收|减征|即征即退|加计扣除"
)
SOFT_BENEFIT_MARKERS = ("减免", "抵减", "优惠")
RATIO_RE = re.compile(r"(?:减按|按)\s*\d+(?:\.\d+)?\s*%")
THRESHOLD_MONEY_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:万|亿)?元(?:以下|以内|不超过)?"
)


def _substance_score(text: str) -> int:
    """一段条文里"实质优惠信息"的浓度。越高越该拿给用户看。

    分档（越靠前越硬）：
      · 写明比例："减按25%…按20%的税率"——直接说明能省多少，10 分
      · 写明额度："不超过300万元"这类门槛，6 分
      · 优惠动词："免征/减半征收"，5 分
      · 泛化说法："减免/优惠"，1 分
    """

    if not text:
        return 0
    score = 0
    score += 10 * len(RATIO_RE.findall(text))
    if "不超过" in text or "以下" in text or "以内" in text:
        if THRESHOLD_MONEY_RE.search(text):
            score += 6
    score += 5 * len(STRONG_BENEFIT_RE.findall(text))
    score += sum(1 for word in SOFT_BENEFIT_MARKERS if word in text)
    return score


# 达到这个分数才算"讲的是优惠实质内容"，才有资格被补充进答案
SUBSTANTIVE_THRESHOLD = 5


def _is_substantive(text: str) -> bool:
    return _substance_score(text) >= SUBSTANTIVE_THRESHOLD


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。；;])", " ".join((text or "").split()))
    return [item.strip() for item in parts if item.strip()]


def _point_text(content: str, limit: int = 120) -> str:
    """取一段条文里最该给用户看的那句。

    先按"实质优惠信息"打分挑，分一样才退回第一句。
    这样"本公告所称小型微利企业，是指…同时符合…三个条件的企业"这类
    真正说明白什么、门槛多少的句子不会被程序性条款挤掉。
    """

    sentences = _split_sentences(content)
    if not sentences:
        return ""
    best = max(sentences, key=_substance_score)
    text = best if _substance_score(best) > 0 else sentences[0]
    return text[:limit] + ("…" if len(text) > limit else "")


def _first_sentence(content: str, limit: int = 90) -> str:
    """取条文的第一句作为"这条讲了什么"的要点。

    为什么截句而不是整段照搬：条文动辄上百字，整段贴进答案会把真正的结论淹没。
    用户要的是"这条管什么"，要看全文可以点开引用卡片。

    """

    text = " ".join((content or "").split())
    if not text:
        return ""
    # 太短的首句没有信息量（增值税法第十条的内容就是"增值税税率:"，
    # 税率表在下一层级的条文里）。这种情况往后多取一句，别给用户一个看不懂的要点。
    minimum = MIN_POINT_CHARS
    for mark in ("。", "；", ";"):
        index = text.find(mark)
        if 0 < index <= limit and index + 1 >= minimum:
            return text[: index + 1]
    return text[:limit] + ("…" if len(text) > limit else "")


def _policy_point(verdict) -> str:
    """把一条适用政策写成人能读的一句话：**条文要点在前，出处备注在后**。

    顺序很重要。写成"《某公告》第三条"是文件清单，用户看完还是不知道政策说了什么；
    写成"小型微利企业按 20% 缴纳企业所得税（出处：财政部 税务总局公告2023年第12号 四、）"
    才是答案 + 备注。这是明确提过的要求。
    """

    point = _point_text(verdict.content) or verdict.title or "（未取到条文内容）"
    source = verdict.citation or verdict.title
    return f"{point}（出处：{source}）" if source else point


def _policy_point_with(verdict, contents: dict[str, str] | None) -> str:
    """同上，但允许用"补全过的正文"（见 _point_contents）。"""

    if contents:
        enriched = contents.get(verdict.citation or "")
        if enriched:
            point = _point_text(enriched) or verdict.title or "（未取到条文内容）"
            source = verdict.citation or verdict.title
            return f"{point}（出处：{source}）" if source else point
    return _policy_point(verdict)


def _merge_reports(target: ApplicabilityReport, extra: ApplicabilityReport) -> None:
    """把补充条文判定结果并进主报告，按引用标签去重。"""

    known = {
        item.citation or item.title
        for item in target.applicable + target.need_confirm + target.not_applicable
    }
    for group in (extra.applicable, extra.need_confirm, extra.not_applicable):
        for item in group:
            key = item.citation or item.title
            if key in known:
                continue
            target.add(item)
            known.add(key)


@dataclass
class AnswerResult:
    """一次问答的完整结果。"""

    question: str
    intent: str
    facts: ExtractedFacts
    template_id: str = ""
    answer: RenderedAnswer | None = None
    applicability: ApplicabilityReport | None = None
    calculation: dict | None = None
    # 本次回答用到的引用（含条文版本 ID、效力状态、生效区间、原文）。
    # 为什么答案里要带上：答案的"政策依据"段只写了文号+条款号这种文字标识，
    # 而界面上要能点开看原文、看效力状态、看是不是还在有效期。
    # 让前端再猜一次"这条依据对应哪条原文"必然出错——依据是这里检索出来的，
    # 原文就该一起给出去。
    citations: list[Citation] = field(default_factory=list)
    # 验证层结论。None 表示未开启验证。
    verification: dict | None = None
    refused: bool = False
    refusal_reason: str = ""
    degraded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "intent": self.intent,
            "facts": self.facts.to_dict(),
            "template_id": self.template_id,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "degraded": list(self.degraded),
            "calculated": self.calculation,
            "verification": self.verification,
            "applicability": self.applicability.to_dict() if self.applicability else None,
            "answer": self.answer.to_dict() if self.answer else None,
            "citations": [item.to_dict() for item in self.citations],
        }

    def to_text(self) -> str:
        return self.answer.to_text() if self.answer else ""


class AnswerPipeline:
    """问答主链路。"""

    def __init__(
        self,
        session: Session,
        *,
        templates_path: str | pathlib.Path | None = None,
        top_n: int = 6,
    ) -> None:
        self.session = session
        self.templates = load_templates(templates_path)
        self.extractor = get_extractor()
        self.judge = ApplicabilityJudge()
        self.calculator = get_engine()
        self.searcher = KnowledgeSearcher(session)
        self.top_n = top_n

    # ------------------------------------------------------------------
    def answer(
        self,
        question: str,
        *,
        tax_type: str | None = None,
        as_of: datetime | None = None,
        verify: bool = True,
    ) -> AnswerResult:
        facts = self.extractor.extract(question)
        intent = classify_intent(question)
        result = AnswerResult(question=question, intent=intent, facts=facts)

        # ② 候选政策召回（时点默认取现在，由检索层负责）
        request = SearchRequest(
            question=question,
            tax_types=[tax_type] if tax_type else list(facts.tax_types),
            as_of=as_of,
            top_n=self.top_n,
        )
        response = self.searcher.search(request)
        result.degraded = list(response.degraded)
        # 引用随答案一起返回：所有分支（追问 / 拒答 / 正常回答）都要带上，
        # 否则"已检索到的相关政策"这类段落在前端就没有可点开的依据。
        result.citations = list(response.citations)

        # ① 要素不全就问，不硬答。
        #    但只对"结论取决于您自身情况"的问题类型生效：
        #    问"小微企业有什么优惠"是要了解政策本身，不该先反问用户是不是小微企业；
        #    问"我要交多少税"则必须先问清身份、金额、口径，否则算出来的数是错的。
        if self._needs_clarification(intent, facts, question):
            return self._clarify(result, facts, response)

        # 没有召回 → 拒答
        if not response.citations:
            return self._refuse(result, "未检索到与问题相关的现行有效条文")

        # ③ 适用性判定
        candidates = [
            {
                "title": citation.regulation_title or citation.document_number or "",
                "citation": citation.label(),
                "content": citation.content or "",
                "tax_types": list(citation.tax_types),
            }
            for citation in response.citations
        ]
        report = self.judge.judge(facts, candidates)
        # 补一轮：同一份文件里"写着比例、额度、条件"的那几条。
        # 检索按相关度排序，常把"通过填写申报表即可享受"这类程序性条款排前面，
        # 真正写着"减半征收""减按25%"的条文反而排在后面甚至没进前 5。
        # 补进来的条文**同样过失用性判定**，不是绕过判定直接给用户。
        extras = self._benefit_candidates(candidates, citations=response.citations, facts=facts)
        if extras:
            extra_report = self.judge.judge(facts, extras)
            _merge_reports(report, extra_report)
        result.applicability = report

        usable = report.applicable + report.need_confirm
        if not usable:
            return self._refuse(
                result,
                "检索到的条文均与您的情况不符（详见不适用清单）",
            )

        # ④ 计算编排（只在意图是算账、且要素够用时才做）
        calculation, calc_context = self._maybe_calculate(facts, intent)
        result.calculation = calculation

        # ⑤ 按模板组装
        contents = self._build_contents(
            facts, report, calculation, self._point_contents(response.citations)
        )
        template = self.templates["finance_tax.answer"]
        rendered = render(template, contents)
        result.template_id = template.template_id
        result.answer = rendered
        if not rendered.ok:
            # 必填段缺失属于生成失败：不返回半成品答案，改走拒答并说明原因
            return self._refuse(result, f"答案生成失败：{rendered.failure_reason}")

        # ⑥ 验证层：错的答案出不去。
        #    放在最后一步，因为它检的是"已经组装好的答案"——
        #    引用有没有编、条文还有没有效、数字和引擎对不对、免责声明在不在。
        #
        #    送检的是**渲染后的段落**，不是组装前的原始内容：
        #    免责声明由模板渲染，不在原始内容里。拿原始内容去验，
        #    会把每一条本来正确的答案都判成"缺免责段"。
        if verify:
            rendered_sections = {section.key: section.content for section in rendered.sections}
            self._verify(
                result, response, facts, rendered_sections, calculation, calc_context, as_of
            )
        return result

    # ------------------------------------------------------------------
    def _verify(
        self,
        result: AnswerResult,
        response,
        facts: ExtractedFacts,
        contents: dict,
        calculation: dict | None,
        calc_context: dict | None,
        as_of: datetime | None,
    ) -> None:
        """送检；验证不过就按 7.3 处理（修正 / 重新生成 / 降级）。"""

        from app.verification import VerificationInput
        from app.verification.pipeline import AnswerVerifier

        payload = VerificationInput(
            question=result.question,
            citations=[
                {
                    "document_number": citation.document_number,
                    "full_no": citation.full_no,
                    "regulation_title": citation.regulation_title,
                    "effect_status": citation.effect_status,
                    "content": citation.content,
                    "regulation_id": citation.regulation_id,
                    "valid_from": citation.valid_from_ts,
                    "valid_to": citation.valid_to_ts,
                }
                for citation in response.citations
            ],
            sections=dict(contents),
            calculation=calculation,
            calculation_context=calc_context,
            as_of=as_of,
        )

        verifier = AnswerVerifier(self.session)
        outcome = verifier.verify_and_repair(payload, rebuild=self._make_rebuilder(payload, facts))
        result.verification = outcome.report.to_dict()

        if outcome.degraded:
            result.degraded.append("verification")
            result.answer = render(self.templates["finance_tax.answer"], outcome.sections)
        elif outcome.report.outcome == "fixed":
            # 数字被复算修正过，答案要用修正后的内容重渲染
            contents.update(outcome.sections)
            result.answer = render(self.templates["finance_tax.answer"], contents)

    def _make_rebuilder(self, payload, facts: ExtractedFacts):
        """重新生成：把验证不过的引用剔掉，用剩下的依据重出一版答案。"""

        def rebuild(inner) -> None:
            kept = {
                (item.get("document_number") or item.get("regulation_title") or "")
                for item in inner.citations
            }
            if not kept:
                raise ValueError("剔掉问题引用后已无可用依据")
            candidates = [
                {
                    "title": item.get("regulation_title") or "",
                    "citation": item.get("document_number") or item.get("regulation_title") or "",
                    "content": item.get("content") or "",
                    "tax_types": [],
                }
                for item in inner.citations
            ]
            from app.reasoning.facts import ExtractedFacts as _Facts

            local = _Facts(raw_text=payload.question)
            local.taxpayer_type = facts.taxpayer_type
            local.taxpayer_type_confirmed = facts.taxpayer_type_confirmed
            local.business_type = facts.business_type
            local.tax_types = list(facts.tax_types)
            report = self.judge.judge(local, candidates)
            inner.sections.update(self._build_contents(local, report, inner.calculation))

        return rebuild

    # ------------------------------------------------------------------
    @staticmethod
    def _needs_clarification(intent: str, facts: ExtractedFacts, question: str = "") -> bool:
        """根据问题类型决定"要不要先追问"。

        reasoner.yaml 的 clarification.required_for_calculation 写的是
        "缺少这些信息就没法算"——注意是**没法算**，不是"没法答"。
        所以只有计算类与办理类需要先补齐要素；
        政策查询类直接讲政策，把"是否适用于您"放进需确认清单。

        但有一种政策查询例外：**问的是"我这种情况"**。
        "小微企业有什么优惠"是了解政策；"我这种情况能享受优惠吗"是要判定——
        后者缺要素就没法判，必须先问清楚。判据是问题里带第一人称。

        第二条例外（2026-10-02 定的口径）：**涉及主体的政策问题，主体不明就先问主体**。
        "小规模纳税人有什么优惠"能直接答；"有什么优惠"不能——优惠按主体分档，
        不问清就给答案，等于让用户自己猜适用哪一档。
        这与"通用政策问题直接答"的旧口径不同，冲突点已记入 ADR-0020 补记。
        """

        if not facts.questions:
            return False
        if intent in {"calculation", "how_to"}:
            return True
        text = question or facts.raw_text or ""
        if any(marker in text for marker in ("我", "我们")):
            return True
        # 主体不明 + 问题依赖主体（优惠 / 能否 / 怎么交）→ 先问主体
        return facts.taxpayer_type is None and any(
            keyword in text for keyword in SUBJECT_DEPENDENT_KEYWORDS
        )

    def _clarify(self, result: AnswerResult, facts: ExtractedFacts, response) -> AnswerResult:
        template = self.templates["finance_tax.clarification"]
        questions = list(facts.questions)
        # 政策类问题只问主体。跟着问"金额是多少"属于多问——
        # 用户问的是"有哪些优惠"，还没到算钱那一步（2026-10-02 的反馈）。
        if result.intent == "policy_query" and facts.subject_question:
            questions = [facts.subject_question]
        contents: dict[str, Any] = {
            "judgement": "为了给出可用的结论，请先确认：" + "；".join(questions),
        }
        if response.citations:
            contents["basis"] = [
                {"text": citation.label(), "effect_status": citation.effect_status}
                for citation in response.citations
            ]
        result.template_id = template.template_id
        result.answer = render(template, contents)
        return result

    def _refuse(self, result: AnswerResult, reason: str) -> AnswerResult:
        """拒答也要按模板输出：说清楚为什么给不了结论、用户可以怎么补。"""

        template = self.templates["finance_tax.clarification"]
        result.refused = True
        result.refusal_reason = reason
        result.template_id = template.template_id
        result.answer = render(
            template,
            {
                "judgement": f"{reason}。您可以补充企业类型、业务内容和期间，或转人工咨询。",
            },
        )
        return result

    # ------------------------------------------------------------------
    def _maybe_calculate(
        self, facts: ExtractedFacts, intent: str
    ) -> tuple[dict | None, dict | None]:
        """需要算、且算得出来时才调用计算引擎。

        算不出来时把"为什么算不了"一并返回（例如身份未确认），
        由答案模板放进风险提示段，而不是悄悄跳过计算。

        第二个返回值是**复算上下文**：验证层要拿同样的输入重算一遍，
        确认答案里的数字确实来自引擎。
        """

        wants_calculation = intent == "calculation" or facts.amount is not None
        if not wants_calculation or facts.amount is None:
            return None, None
        if "增值税" not in facts.tax_types and facts.tax_types:
            return None, None

        vat = self.calculator.calc_vat(
            VatInput(
                taxpayer_type=facts.taxpayer_type,
                business_type=facts.business_type,
                sales_amount=facts.amount,
                amount_includes_tax=bool(facts.amount_includes_tax),
                input_vat=facts.input_vat,
                period_scope=facts.period_scope,
            )
        )
        payload: dict[str, Any] = {"vat": vat.to_dict()}
        if vat.payable > 0:
            surcharge = self.calculator.calc_surcharges(
                SurchargeInput(
                    vat_payable=vat.payable,
                    location=facts.region,
                    taxpayer_type=facts.taxpayer_type,
                )
            )
            payload["surcharges"] = surcharge.to_dict()
        payload["notes"] = list(vat.notes)
        context = {
            "taxpayer_type": facts.taxpayer_type,
            "taxpayer_type_confirmed": facts.taxpayer_type_confirmed,
            "business_type": facts.business_type,
            "amount": str(facts.amount),
            "amount_includes_tax": facts.amount_includes_tax,
            "input_vat": str(facts.input_vat) if facts.input_vat is not None else None,
            "region": facts.region,
            "period_scope": facts.period_scope,
        }
        return payload, context

    def _build_contents(
        self,
        facts: ExtractedFacts,
        report: ApplicabilityReport,
        calculation: dict | None,
        point_contents: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        judgement = _build_judgement(facts, report, calculation, point_contents)

        contents: dict[str, Any] = {
            "judgement": judgement,
            "basis": [
                {
                    "text": item.citation or item.title,
                    "verdict": item.verdict.value,
                    "reason": item.reason,
                }
                for item in (report.applicable + report.need_confirm + report.not_applicable)
            ],
        }

        if calculation:
            steps: list[dict] = []
            for block_name in ("vat", "surcharges"):
                block = calculation.get(block_name)
                if not block:
                    continue
                for step in block.get("steps", []):
                    steps.append(
                        {
                            "title": step["title"],
                            "formula": step["formula"],
                            "substitution": step["substitution"],
                            "result": step["result"],
                            "citation": step.get("citation", ""),
                        }
                    )
            contents["calculation"] = steps

        risks: list[str] = list(calculation.get("notes", [])) if calculation else []
        risks.extend(item.reason for item in report.need_confirm)
        risks.extend(
            f"{item.citation or item.title}：{item.reason}" for item in report.not_applicable[:3]
        )
        # 空串要去掉。曾经出现过"风险提示"里挂着三条空项目符号——
        # 那比没有这一栏更糟：用户会以为"这里有内容但没显示出来"。
        cleaned = [item.strip() for item in dict.fromkeys(risks) if item and item.strip()]
        if cleaned:
            contents["risks"] = cleaned

        return contents

    # ------------------------------------------------------------------
    def _benefit_candidates(
        self,
        existing: list[dict],
        *,
        citations,
        facts: ExtractedFacts,
        per_regulation: int = 3,
        total_limit: int = 5,
    ) -> list[dict]:
        """找出"同一份文件里写着比例、额度、条件"的条文，作为补充候选。

        为什么需要这一步：用户问"小微企业有什么税收优惠"，检索回来的前 5 条
        经常是《2023年第6号》的"通过填写纳税申报表即可享受"这类**程序性条款**，
        而真正写着"减按25%计算应纳税所得额，按20%的税率缴纳"
        "减半征收资源税、城市维护建设税…"的《2023年第12号》第二条、第三条
        反而排在后面。结果是答案看了半天，不知道到底能省多少。

        筛选口径（三道，全部满足才补）：
          1. 含实质优惠信息（比例、金额门槛、免征/减半/减按这类词）；
          2. 提到用户的主体（避免把"个体工商户"的优惠塞给"小型微利企业"）；
          3. 不含已在候选里的条文。

        补进来的条文**同样过一遍适用性判定**（见调用处），
        不是绕过条件检查直接写进答案。
        """

        from sqlalchemy import select

        from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

        seen = {item.get("citation", "") for item in existing}
        subject = facts.taxpayer_type or ""

        regulation_ids: list[str] = []
        for citation in citations:
            regulation_id = getattr(citation, "regulation_id", None)
            if regulation_id and regulation_id not in regulation_ids:
                regulation_ids.append(regulation_id)
            if len(regulation_ids) >= 3:
                break

        extras: list[dict] = []
        for regulation_id in regulation_ids:
            rows = self.session.execute(
                select(RegulationArticle, RegulationArticleVersion, Regulation)
                .join(
                    RegulationArticleVersion,
                    RegulationArticleVersion.article_id == RegulationArticle.id,
                )
                .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
                .where(
                    RegulationArticle.regulation_id == regulation_id,
                    # 可引用层级就是这三个（见检索层的 INDEXABLE_LEVELS）。
                    # 注意公告类的"一、二、三、"在这里是 item，不是 article——
                    # 只查 article/paragraph 会把《2023年第12号》整份文件漏掉。
                    RegulationArticle.level_code.in_(("article", "paragraph", "item")),
                )
                .order_by(RegulationArticle.order_index)
                .limit(80)
            ).all()

            added = 0
            for article, version, regulation in rows:
                if len(extras) >= total_limit or added >= per_regulation:
                    break
                content = (version.content or "").strip()
                if not _is_substantive(content):
                    continue
                if subject and subject not in content:
                    continue
                head = regulation.document_number or regulation.title
                label = f"{head} {article.full_no}".strip()
                if label in seen:
                    continue
                extras.append(
                    {
                        "title": regulation.title,
                        "citation": label,
                        "content": content,
                        "tax_types": list(regulation.tax_types or []),
                    }
                )
                seen.add(label)
                added += 1
            if len(extras) >= total_limit:
                break
        return extras

    # ------------------------------------------------------------------
    def _point_contents(self, citations) -> dict[str, str]:
        """把"只有标题"的条文补全成能读懂的要点。返回 {引用标签: 正文}。

        为什么需要：像《增值税法》第十条，条文正文就一句"增值税税率:"，
        真正的税率写在它下面的（一）（二）各款里。只把标题当要点交给用户，
        等于给了一个看不懂的答案。

        为什么不在检索层做：检索只要判断"这条命中没命中"，不需要更长的正文；
        而答案要的是"这条讲了什么"。两者分开，检索结果就不会被改写。
        """

        from sqlalchemy import select

        from app.models.knowledge import RegulationArticle, RegulationArticleVersion

        enriched: dict[str, str] = {}
        for citation in citations:
            content = (citation.content or "").strip()
            if (
                len(content) >= MIN_POINT_CHARS
                or not citation.regulation_id
                or not citation.full_no
            ):
                continue
            children = self.session.execute(
                select(RegulationArticleVersion.content)
                .join(
                    RegulationArticle,
                    RegulationArticle.id == RegulationArticleVersion.article_id,
                )
                .where(
                    RegulationArticle.regulation_id == citation.regulation_id,
                    RegulationArticle.full_no.like(f"{citation.full_no} %"),
                )
                .order_by(RegulationArticle.order_index)
                .limit(3)
            ).scalars().all()
            parts = [item.strip() for item in children if item and item.strip()]
            if parts:
                enriched[citation.label()] = content + " " + " ".join(parts)
        return enriched

def _build_judgement(
    facts: ExtractedFacts,
    report: ApplicabilityReport,
    calculation: dict | None,
    point_contents: dict[str, str] | None = None,
) -> str:
    """组装「情形判定」——**这里必须是答案，不是文件清单**。

    曾经的做法是"可以适用：文号A；文号B；文号C"，用户看到的就是一串文件名，
    既不知道政策讲了什么，也不知道自己该怎么办。用户的反馈很直接：
    "应该是你给我答案，完了备注说明依据哪些文件"。

    也不复述用户刚说过的话。第二次反馈是"回答过于刻意……
    问题已经说了小微企业，那就直接回答小微企业的税收优惠政策"。
    所以：**用户说过的信息不再重复**，答案直接开场。

    现在的顺序是：
      ① 能算出数就先把结论顶到最前面（同时交代用的是哪几个参数，便于核对）
      ② 每条适用政策给出**条文要点**（原文首句），出处放在后面的括号里
      ③ 只有"从线索推测出来的身份"才需要提醒一句尚未确认
      ④ 需要确认的、已排除的，各自一句话讲清
    """

    lines: list[str] = []
    question = facts.raw_text or ""

    # 结论优先：能算出来就先把数给出去，计算过程在后一段展开
    vat_block = (calculation or {}).get("vat") or {}
    payable = vat_block.get("payable")
    computed = payable is not None and bool(vat_block.get("steps"))
    preferences = vat_block.get("preferences") or []

    basis_bits: list[str] = []
    if facts.amount is not None:
        amount_text = f"{money_text(facts.amount)} 元"
        if facts.amount_includes_tax is True:
            amount_text += "（含税）"
        elif facts.amount_includes_tax is False:
            amount_text += "（不含税）"
        basis_bits.append(amount_text)
    if facts.period:
        # 期间必须写出来：它决定按月度还是季度判断免税额，
        # 不写出前提，用户没法核对系统是不是按他说的那个期间算的。
        basis_bits.append(str(facts.period))
    if facts.business_type:
        basis_bits.append(facts.business_type)
    if computed:
        # 先讲政策再讲数：用户的原话是"应该是先查看税收政策，包括优惠政策，
        # 然后按照政策计算税款"。套用了优惠就要把优惠和依据说在前头。
        if preferences:
            first = preferences[0]
            lines.append(f"按现行政策：{first.get('name', '')}（{first.get('citation', '')}）。")
        if basis_bits:
            lines.append("按" + "、".join(basis_bits) + "计算：")
        lines.append(
            f"应纳增值税 {money_text(Decimal(str(payable)))} 元"
            "（计算过程见下一段）。"
        )

    # 只有"从线索推测"才提醒。用户自己说了主体就不再复述——
    # 复述一遍"您说的是小规模纳税人"只让人觉得机器在念稿。
    if not facts.taxpayer_type_stated and facts.taxpayer_type:
        lines.append(f"根据您的描述，您可能是{facts.taxpayer_type}（尚未确认）。")

    # 计算类问题：政策清单收敛到"这次计算真正用到的依据"。
    # 检索回来的条款里混着"一般纳税人登记""一般计税方法"这类与本题无关的内容，
    # 把它们列在"可以适用"里，用户会以为自己也适用——那不是答案，是噪音。
    used_citations: list[str] = []
    if computed:
        for block_name in ("vat", "surcharges"):
            block = (calculation or {}).get(block_name) or {}
            for step in block.get("steps") or []:
                text = str(step.get("citation") or "").strip()
                if text:
                    used_citations.append(text)
            for text in block.get("citations") or []:
                text = str(text).strip()
                if text:
                    used_citations.append(text)
        used_citations = list(dict.fromkeys(used_citations))

    if computed and used_citations:
        lines.append("")
        lines.append("本次计算依据的政策：")
        for text in used_citations[:5]:
            lines.append(f"· {text}")
    elif report.applicable:
        if lines:
            lines.append("")
        asking_benefits = any(word in question for word in ("优惠", "减免", "免征", "减半"))
        # 问"有什么优惠"时，把写着比例/额度/条件的那几条排到前面——
        # 用户要的是"能省多少"，不是"怎么办手续"。
        applicable = list(report.applicable)
        if asking_benefits:
            applicable.sort(key=lambda item: -_substance_score(item.content))
        # 按实际列出的条数说，别写"共 6 项"却只列出 5 条
        shown = applicable[:5]
        if asking_benefits:
            lines.append(f"可以享受的税收优惠主要有 {len(shown)} 项：")
        else:
            lines.append(f"可以适用以下 {len(shown)} 条现行有效政策：")
        for item in shown:
            lines.append(f"· {_policy_point_with(item, point_contents)}")

    if report.need_confirm:
        missing = [item.reason for item in report.need_confirm if item.reason]
        if missing:
            lines.append("")
            lines.append(
                f"另有 {len(report.need_confirm)} 条政策要等您补充信息后才能判断："
                + "；".join(dict.fromkeys(missing[:3]))
            )

    if report.not_applicable:
        lines.append("")
        lines.append(
            f"已排除 {len(report.not_applicable)} 条（与您的情况不符）："
            + "；".join(
                f"{item.citation or item.title}（{item.reason}）"
                for item in report.not_applicable[:3]
            )
        )

    return "\n".join(lines).strip()

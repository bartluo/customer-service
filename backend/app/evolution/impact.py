"""影响分析：新文件影响了哪些已有知识。

技术方案 8.2 的三条判断：
  ① 是否废止/修订了已入库的某条款？ → 触发相关性复审
  ② 是否新增了某场景的规定？       → 生成候选知识
  ③ 是否只是解读性文章？           → 归档，不作依据

实现方式是**从新文件正文里找被点名的既有法规**：
    "《中华人民共和国增值税暂行条例》同时废止"
    "对《XX办法》作如下修改"
这类句子在财税文件里写法固定，用规则抽取比模型可靠得多。

判断"解读性文章"：正文里没有条款结构、也没有废止/修订表述，
而且标题带"解读""问答""答记者问"——这类归档入库，不作为依据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import Regulation

# 废止 / 修订的表述。中文法规里就这几种写法。
REPEAL_PATTERNS = (
    re.compile(r"《(?P<title>[^》]{4,60})》\s*(?:同时)?\s*废止"),
    re.compile(r"废止\s*《(?P<title>[^》]{4,60})》"),
    re.compile(r"《(?P<title>[^》]{4,60})》\s*(?:同时)?\s*失效"),
)
AMEND_PATTERNS = (
    re.compile(r"对\s*《(?P<title>[^》]{4,60})》\s*(?:作|进行)\s*(?:如下)?\s*(?:修改|修订)"),
    re.compile(r"《(?P<title>[^》]{4,60})》\s*(?:作|进行)\s*(?:如下)?\s*(?:修改|修订)"),
    re.compile(r"修改\s*《(?P<title>[^》]{4,60})》"),
)

# 解读性文章的特征（归档，不作依据）
INTERPRETATION_HINTS = ("解读", "答记者问", "问答", "政策指引", "操作指南")


@dataclass
class ImpactedRegulation:
    """一条受影响的已有法规。"""

    regulation_id: str
    title: str
    document_number: str | None
    relation: str  # repealed / amended
    evidence: str

    def to_dict(self) -> dict:
        return {
            "regulation_id": self.regulation_id,
            "title": self.title,
            "document_number": self.document_number,
            "relation": self.relation,
            "evidence": self.evidence,
        }


@dataclass
class ImpactAnalysis:
    """一次影响分析的结果。"""

    kind: str = "new_scenario"  # repealed / amended / new_scenario / interpretation
    impacted: list[ImpactedRegulation] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """是否触发复审。"""

        return self.kind in {"repealed", "amended"} or bool(self.impacted)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "needs_review": self.needs_review,
            "reasons": list(self.reasons),
            "impacted": [item.to_dict() for item in self.impacted],
        }

    def explain(self) -> str:
        lines = [f"影响类型：{self.kind}"]
        for reason in self.reasons:
            lines.append(f"  · {reason}")
        for item in self.impacted:
            lines.append(
                f"  → 受影响：{item.title}"
                f"（{'被废止' if item.relation == 'repealed' else '被修订'}）"
            )
            lines.append(f"      依据原文：“{item.evidence}”")
        if not self.impacted and self.kind == "new_scenario":
            lines.append("  · 未点名任何既有法规，按「新增场景」处理，生成候选知识")
        return "\n".join(lines)


def analyze_impact(
    session: Session,
    *,
    title: str,
    content: str,
    document_number: str | None = None,
) -> ImpactAnalysis:
    """分析一份新文件对已有知识的影响。"""

    analysis = ImpactAnalysis()
    text = content or ""

    repeal_hits = _collect(text, REPEAL_PATTERNS)
    amend_hits = _collect(text, AMEND_PATTERNS)

    if repeal_hits:
        analysis.kind = "repealed"
        analysis.reasons.append(f"正文点名废止 {len(repeal_hits)} 部既有法规，需触发复审")
    elif amend_hits:
        analysis.kind = "amended"
        analysis.reasons.append(f"正文点名修订 {len(amend_hits)} 部既有法规，需触发复审")
    elif any(hint in (title or "") for hint in INTERPRETATION_HINTS):
        analysis.kind = "interpretation"
        analysis.reasons.append("标题属于解读/问答类文章，归档留存、不作为引用依据")
        return analysis
    else:
        analysis.kind = "new_scenario"
        analysis.reasons.append("未见废止或修订表述，按新增场景处理")

    for relation, hits in (("repealed", repeal_hits), ("amended", amend_hits)):
        for named_title, evidence in hits:
            regulation = _find_regulation(session, named_title)
            if regulation is None:
                analysis.reasons.append(f"被点名的《{named_title}》不在知识库内，跳过")
                continue
            analysis.impacted.append(
                ImpactedRegulation(
                    regulation_id=regulation.id,
                    title=regulation.title,
                    document_number=regulation.document_number,
                    relation=relation,
                    evidence=evidence,
                )
            )
    return analysis


def _collect(text: str, patterns) -> list[tuple[str, str]]:
    """抽取被点名的法规标题与原文片段（同一标题只取一次）。"""

    found: dict[str, str] = {}
    for pattern in patterns:
        for match in pattern.finditer(text or ""):
            name = match.group("title").strip()
            if name and name not in found:
                found[name] = match.group(0).strip()
    return list(found.items())


def _find_regulation(session: Session, named_title: str) -> Regulation | None:
    """按标题找法规，容忍书名号内的空格与标点差异。"""

    normalized = re.sub(r"[\s（）()、，,]+", "", named_title)
    rows = session.execute(select(Regulation)).scalars().all()
    for row in rows:
        if re.sub(r"[\s（）()、，,]+", "", row.title or "") == normalized:
            return row
    # 退一步：包含匹配（修订决定里常带"（试行）"之类的后缀差异）
    for row in rows:
        row_title = re.sub(r"[\s（）()、，,]+", "", row.title or "")
        if row_title and (row_title in normalized or normalized in row_title):
            return row
    return None

"""法规关系抽取。

回答"顺着查"的能力来源。有了这些边，系统才能回答：
  · 这条规定是依据哪部上位法制定的？（based_on）
  · 这条被哪份文件改了？（amended_by）
  · 这条什么时候被谁废了？（repealed_by）
  · 这个优惠有什么前提条件？（references）

设计原则：宁可少抽，不可乱抽。每条关系都带 evidence（原文证据）与
confidence（可信度），审核专家能一眼看出对不对。抽错的关系比抽不到更有害——
它会让系统顺着错误的边给出错误答案。
"""

from __future__ import annotations
import re
from dataclasses import dataclass

from app.knowledge.versioning import _DOC_NUMBER_TOKEN, detect_repeal_targets

# "根据《xxx法》制定" / "依据《xxx条例》" → based_on
_BASED_ON_RE = re.compile(
    r"(?:根据|依据|按照)\s*[《【]?\s*(?P<name>[^》】。，；\s]{4,60}?)\s*[》】]?\s*(?:制定|的规定|的规定制定|执行)"
)

# 《中华人民共和国增值税暂行条例》这类书名号内的法规名
_LAW_TITLE_RE = re.compile(r"[《【]\s*(?P<name>[^》】]{4,60})\s*[》】]")

# "见附件一" / "按照第三条" → references
_REFERENCES_RE = re.compile(r"(?:见|详见|按照|依照)\s*(?P<target>附件[一二三四五六七八九十\d]+|第[一二三四五六七八九十百零〇\d]+条)")

# "取代 / 替代" → amends
_SUPERSEDE_RE = re.compile(r"(?:取代|替代)")


@dataclass(frozen=True)
class ExtractedRelation:
    """一条抽取到的关系。"""

    relation_type: str
    target_ref: str
    evidence: str
    confidence: float


def _sentence_split(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。；;\n]", text) if s.strip()]


def extract_relations(text: str, source_document_number: str | None = None) -> list[ExtractedRelation]:
    """从法规正文抽取关系边。

    source_document_number 是本文件的文号，用于排除"自己引用自己"。
    返回的 target_ref 是原始提及文本，抽取不出精确 ID 时保留原文供人工核对。
    """

    if not text:
        return []

    results: list[ExtractedRelation] = []
    seen: set[tuple[str, str]] = set()

    def add(relation_type: str, target_ref: str, evidence: str, confidence: float) -> None:
        key = (relation_type, target_ref)
        if key in seen:
            return
        seen.add(key)
        results.append(
            ExtractedRelation(
                relation_type=relation_type,
                target_ref=target_ref,
                evidence=evidence,
                confidence=confidence,
            )
        )

    # 1. 废止关系：置信度最高，措辞最明确
    for target in detect_repeal_targets(text):
        add("repeals", target["document_number"], target["evidence"], 0.95)

    for sentence in _sentence_split(text):
        # 2. 上位法关系：书名号内的法规名 + "根据/依据...制定"
        based = _BASED_ON_RE.search(sentence)
        if based:
            add("based_on", based.group("name"), sentence, 0.9)

        # 3. 引用关系：见附件、按第几条
        references = _REFERENCES_RE.search(sentence)
        if references:
            add("references", references.group("target"), sentence, 0.8)

        # 4. 替代关系：取代/替代
        if _SUPERSEDE_RE.search(sentence):
            for match in _LAW_TITLE_RE.finditer(sentence):
                add("amends", match.group("name"), sentence, 0.85)
            for match in _DOC_NUMBER_TOKEN.finditer(sentence):
                add("amends", match.group("number"), sentence, 0.85)

    # 排除自己引用自己
    if source_document_number:
        results = [
            r
            for r in results
            if re.sub(r"\s+", "", r.target_ref)
            != re.sub(r"\s+", "", source_document_number)
        ]

    return results

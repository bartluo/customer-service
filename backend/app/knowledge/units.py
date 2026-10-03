"""知识单元抽取。

把"条文"变成"能独立回答一个问题的知识单元"。

为什么需要这一步：用户问"小规模纳税人现在交多少税"，
检索可能召回《增值税暂行条例》第八条——它是正确的依据，但它本身不是答案。
知识单元要补上"这个问题问的是什么""答案一句话怎么说"，
让检索和回答阶段少做一层翻译。

可用性评分（usability）的作用是筛选：像"依照有关规定执行"这种
空壳条文抽出来也没用，评分低，会被排在后面或直接过滤。
"""

from __future__ import annotations
import re
from dataclasses import dataclass

from app.knowledge.parser import ParsedRegulation

# 可引用单位：条 / 款 / 项
_CITABLE_LEVELS = {"article", "paragraph", "item"}

# 空壳条文特征：没有实质内容，只有指向性表述
_VAGUE_MARKERS = (
    "依照有关规定执行",
    "按照有关规定办理",
    "依照相关规定执行",
    "按有关规定处理",
    "由省级税务机关确定",
    "另行规定",
    "参照执行",
)

# 含具体数字/比例/条件的条文通常更可答
_CONCRETE_PATTERNS = (
    re.compile(r"\d+(\.\d+)?%"),
    re.compile(r"\d+元"),
    re.compile(r"第[一二三四五六七八九十百零〇\d]+条"),
    re.compile(r"(?:适用|可以|不得|应当|减免|减按|免征|征收)"),
)


@dataclass
class KnowledgeUnit:
    """一个可独立回答的知识单元。"""

    level_code: str
    source_no: str
    content: str
    question_hint: str
    answer_summary: str
    usability: float


def score_unit_usability(content: str) -> float:
    """给条文内容打可用性分数（0～1）。

    评分维度：
      · 空壳表述（"依照有关规定执行"）→ 直接判低分，这类抽出来也没用
      · 含具体数字/比例/条件 → 加分，因为用户问题通常就落在这里
      · 长度适中 → 加分，太短说不清，太长不像一个独立知识点
    """

    text = content.strip()
    if not text:
        return 0.0

    for marker in _VAGUE_MARKERS:
        if marker in text:
            return 0.1

    score = 0.4
    if any(pattern.search(text) for pattern in _CONCRETE_PATTERNS):
        score += 0.3
    if 15 <= len(text) <= 200:
        score += 0.2
    elif len(text) > 400:
        score -= 0.1

    return round(min(score, 1.0), 2)


def _question_hint(content: str) -> str:
    """从条文推断一个典型提问句，作为检索与召回的辅助信号。

    这是一个轻量启发式，不是 NLP：把陈述句转成"什么/谁/怎么"+内容。
    它不参与答案生成，只用于提高召回与排序质量。
    """

    text = content.strip()
    if not text:
        return ""
    if "税率" in text:
        return f"{text[:20]}…适用什么税率？"
    if "减按" in text or "免征" in text or "减免" in text:
        return f"{text[:20]}…有什么税收优惠？"
    if "适用" in text:
        return f"{text[:20]}…适用范围是什么？"
    return f"{text[:20]}…如何处理？"


def _answer_summary(content: str) -> str:
    """一句话结论：取首句（中文句号分句），过长则截断。"""

    first_sentence = re.split(r"[。；;]", content.strip())[0]
    if len(first_sentence) <= 120:
        return first_sentence
    return first_sentence[:117] + "…"


def extract_knowledge_units(parsed: ParsedRegulation) -> list[KnowledgeUnit]:
    """从解析结果中提炼知识单元。

    只处理 条 / 款 / 项 三级——章、节是目录结构，不含可引用正文。
    按可用性降序返回，方便下游直接取 Top-N。
    """

    units: list[KnowledgeUnit] = []

    for block in parsed.blocks:
        if block.level_code not in _CITABLE_LEVELS:
            continue
        content = block.content.strip()
        if not content:
            continue

        usability = score_unit_usability(content)
        units.append(
            KnowledgeUnit(
                level_code=block.level_code,
                source_no=block.full_no or block.article_no,
                content=content,
                question_hint=_question_hint(content),
                answer_summary=_answer_summary(content),
                usability=usability,
            )
        )

    units.sort(key=lambda u: u.usability, reverse=True)
    return units

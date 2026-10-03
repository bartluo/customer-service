"""术语扩展（第三路）。

用户说"小规模"，法规里写"小规模纳税人"；用户说"进项"，法规里写"进项税额"。
不扩展就召回不了——向量模型对这种半个词的差异并不敏感，
但对用户来说这是同一件事。

为什么不用向量模型做查询扩展（generative query expansion）：
  · 财税术语的映射是有限且已知的，规则表可审计、可给审核专家维护；
  · 模型扩展会"编"出法规里不存在的术语，财税场景这是硬伤。

扩展策略保守：只替换已登记的术语，不做同义词猜测。
宁可少召回，不可召回错的依据。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field

from app.domain.tax_types import detect_tax_types

logger = logging.getLogger(__name__)


@dataclass
class GlossaryTerm:
    """一条术语映射。"""

    alias: str           # 用户口语说法
    canonical: str       # 法规里的正式表述
    tax_types: list[str] = field(default_factory=list)


# 财税域首版术语表。维护入口在 domains/finance_tax/glossary.yaml，
# 会接成可由审核专家在线维护的界面。
DEFAULT_TERMS: list[GlossaryTerm] = [
    GlossaryTerm("小规模", "小规模纳税人", ["增值税"]),
    GlossaryTerm("一般纳税人", "一般纳税人", ["增值税"]),
    GlossaryTerm("进项", "进项税额", ["增值税"]),
    GlossaryTerm("销项", "销项税额", ["增值税"]),
    GlossaryTerm("留抵", "留抵税额", ["增值税"]),
    GlossaryTerm("抵扣", "进项税额抵扣", ["增值税"]),
    GlossaryTerm("普票", "增值税专用发票", ["增值税"]),
    GlossaryTerm("专票", "增值税专用发票", ["增值税"]),
    GlossaryTerm("普票和专票", "增值税专用发票", ["增值税"]),
    GlossaryTerm("所得税", "企业所得税", ["企业所得税"]),
    GlossaryTerm("个税", "个人所得税", ["个人所得税"]),
    # 不能只写"附加"：它会把"专项附加扣除"里的"附加"也算成城建税附加，
    # 于是用户问个税专项附加扣除，过滤器里却多了城市维护建设税。
    # 写成完整的词才安全。
    GlossaryTerm("附加税费", "城市维护建设税", ["城市维护建设税"]),
    GlossaryTerm("附加税", "城市维护建设税", ["城市维护建设税"]),
    GlossaryTerm("教育费附加", "教育费附加", []),
    GlossaryTerm("印花", "印花税", ["印花税"]),
    GlossaryTerm("所得税汇算", "企业所得税年度纳税申报", ["企业所得税"]),
    GlossaryTerm("报税", "纳税申报", []),
    GlossaryTerm("开发票", "发票开具", ["增值税"]),
    GlossaryTerm("认证", "发票认证", ["增值税"]),
    GlossaryTerm("滞纳", "滞纳金", []),
    GlossaryTerm("查账", "税务稽查", []),
    GlossaryTerm("税筹", "税收筹划", []),
    GlossaryTerm("核定征收", "核定征收方式", []),
    GlossaryTerm("查账征收", "查账征收方式", []),
    # 社保与残保金不是税，但客户都是按这两个简称来问的
    GlossaryTerm("社保", "社会保险费", ["社会保险费"]),
    GlossaryTerm("五险", "社会保险费", ["社会保险费"]),
    GlossaryTerm("残保金", "残疾人就业保障金", ["残疾人就业保障金"]),
]


class GlossaryExpander:
    """把口语术语扩展成法规术语。"""

    def __init__(self, terms: list[GlossaryTerm] | None = None) -> None:
        self.terms = terms if terms is not None else list(DEFAULT_TERMS)
        # 长别名优先：先替换"普票和专票"，再替换"普票"
        self._ordered = sorted(self.terms, key=lambda term: len(term.alias), reverse=True)

    def expand(self, text: str) -> str:
        """返回扩展后的查询串。原文保留，扩展词追加在后面。

        保留原文是因为"进项"是原文里真实出现的词，
        删掉换成"进项税额"反而丢信息；追加则是让两套说法都能被匹配到。
        """

        if not text:
            return text
        added: list[str] = []
        for term in self._ordered:
            if term.alias in text and term.canonical not in text:
                added.append(term.canonical)
        if not added:
            return text
        expanded = f"{text} {' '.join(added)}"
        logger.debug("术语扩展：%s -> %s", text, expanded)
        return expanded

    def expand_tax_types(self, text: str) -> list[str]:
        """从查询串里推断可能涉及的税种，用于加税种过滤。

        两步走，顺序不能反：
          1. 直接识别税种全名（个人所得税、土地增值税、房产税……）
          2. 再从已登记的术语别名补充（"小规模"→增值税、"进项"→增值税）

        第 1 步必须先把命中的全名从查询串里挖掉，否则
        "个人所得税"里的"所得税"会被术语表的"所得税"（→企业所得税）抢先匹配，
        用户问个税、系统却去搜企业所得税的条文——这是实际发生过的错。

        为什么要补第 1 步：术语表只登记了几十个口语别名，
        用户直接写税种全名（非常常见）时，旧实现一个都认不出来，
        等于没加过滤，检索结果就飘到别的税种去了。
        """

        if not text:
            return []

        found = detect_tax_types(text)

        # 把已识别的税种全名挖掉，剩下的部分才交给别名匹配
        remaining = text
        for tax_type in found:
            remaining = remaining.replace(tax_type, " ")

        for term in self._ordered:
            if term.alias in remaining:
                for tax_type in term.tax_types:
                    if tax_type not in found:
                        found.append(tax_type)
        return found

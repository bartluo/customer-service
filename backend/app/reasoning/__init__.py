"""推理层（技术方案 4.4）。

推理五步里本模块负责前三步：
  1. 事实结构化——把"我这小店卖了200万"变成结构化要素
  2. 候选政策召回        ——由 retrieval 层负责
  3. 适用性判定——对每个候选政策给出"适用 / 不适用 / 需确认"

一条贯穿全层的原则：**抽不到就说抽不到**。
  财税场景里"猜用户情况"比"多问一句"危险得多——猜错了用户会按错误的结论去申报。
  所以缺失要素一律进"待补清单"，由调用方决定是追问还是标注"需确认"。
"""

from app.reasoning.applicability import (
    ApplicabilityJudge,
    ApplicabilityReport,
    Verdict,
)
from app.reasoning.facts import ExtractedFacts, FactExtractor
from app.reasoning.pipeline import AnswerPipeline, AnswerResult, classify_intent
from app.reasoning.templates import AnswerTemplate, RenderedAnswer, load_templates, render

__all__ = [
    "AnswerPipeline",
    "AnswerResult",
    "AnswerTemplate",
    "ApplicabilityJudge",
    "ApplicabilityReport",
    "ExtractedFacts",
    "FactExtractor",
    "RenderedAnswer",
    "Verdict",
    "classify_intent",
    "load_templates",
    "render",
]

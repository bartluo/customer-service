"""税费计算引擎（技术方案 4.5）。

铁律：涉及金额、税率、期限的计算，一律由本引擎完成，不允许模型心算。
大模型的算术错误率足以摧毁专业信任，而且它算错时还会写得很有自信。

三条设计约束（都来自技术方案 4.5）：
  1. 规则配置化：税率、公式、优惠条件写在 domains/finance_tax/tax_rules.yaml，
     改规则不改代码。
  2. 可展示过程：返回的不是一个数字，而是"公式 + 代入 + 中间值"的步骤列表。
  3. 精确小数：用 Decimal 而不是 float（0.1 + 0.2 这类误差在财税场景不可接受）。
"""

from app.calculation.engine import CalculationEngine, get_engine
from app.calculation.result import CalcResult, CalcStep
from app.calculation.rules import RuleSet, RuleSetError, load_rules

__all__ = [
    "CalcResult",
    "CalcStep",
    "CalculationEngine",
    "RuleSet",
    "RuleSetError",
    "get_engine",
    "load_rules",
]

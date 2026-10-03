"""验证层（技术方案 6）。

为什么专业域必须有这一层：
  普通客服答错了，用户顶多不满意；专业域答错了，后果是补税、滞纳金、
  罚款、信用降级。**"生成完就直接返回"在专业域不可接受。**

四类验证器（7.2）：
  · 引用验证器   —— 引用的文号、条款号真实存在吗（一票否决：引用错误是编造依据）
  · 时效验证器   —— 引用的版本在问题时点有效吗（财税的绝对红线）
  · 计算复算器   —— 答案里的数字与计算引擎复算的结果一致吗
  · 合规验证器   —— 免责声明齐全吗、有没有越界表述

失败处理（7.3）：能修正就修正，不能就带约束重新生成（最多两次），
再不行就降级输出——**宁可说"不确定"，也不输出未通过验证的结论。**
"""

from app.verification.calculation import CalculationVerifier
from app.verification.citation import CitationVerifier
from app.verification.compliance import ComplianceVerifier
from app.verification.pipeline import AnswerVerifier, VerificationOutcome
from app.verification.result import (
    Issue,
    Severity,
    VerifierResult,
    VerificationInput,
    VerificationReport,
)
from app.verification.temporal import TemporalVerifier

__all__ = [
    "AnswerVerifier",
    "CalculationVerifier",
    "CitationVerifier",
    "ComplianceVerifier",
    "Issue",
    "Severity",
    "TemporalVerifier",
    "VerificationInput",
    "VerificationOutcome",
    "VerificationReport",
    "VerifierResult",
]

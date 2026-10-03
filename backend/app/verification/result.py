"""验证层的数据结构。

设计的核心是"问题（Issue）"这个概念：每次验证产出的不是"通过/不通过"，
而是**一串带严重级别的问题**。原因：
  · 失败处理流程（7.3）要按问题类型分流——能修正的、要重新生成的、只能降级的；
  · 验证记录（7.4）要按原因分类统计，才能看出系统性问题（比如总引用某份老文件）；
  · 只给一个布尔值，出事之后查不出"到底哪儿错了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """问题严重级别。"""

    BLOCKING = "blocking"  # 命中即阻断输出（引用不存在、引用废止条文）
    FIXABLE = "fixable"  # 可以自动修正（数字与引擎不一致、引用格式错）
    WARNING = "warning"  # 不影响输出，但要记录（表述偏绝对等）


@dataclass
class Issue:
    """一个验证问题。"""

    verifier: str  # citation / temporal / calculation / compliance
    severity: Severity
    code: str  # 机器可读的原因码，用于统计
    message: str  # 人可读说明
    target: str = ""  # 出问题的引用或数字
    fix: str = ""  # 修正建议（可修正的问题才有）

    def to_dict(self) -> dict:
        return {
            "verifier": self.verifier,
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "target": self.target,
            "fix": self.fix,
        }


@dataclass
class VerifierResult:
    """单个验证器的结果。"""

    name: str
    checked: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(issue.severity is Severity.BLOCKING for issue in self.issues)

    @property
    def has_fixable(self) -> bool:
        return any(issue.severity is Severity.FIXABLE for issue in self.issues)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "checked": self.checked,
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class VerificationInput:
    """送检的答案。"""

    question: str = ""
    # 答案里引用的条文。每条：{document_number, full_no, regulation_title,
    # effect_status, content, source_url, regulation_id}
    citations: list[dict] = field(default_factory=list)
    # 结构化答案段落 {section_key: content}
    sections: dict[str, Any] = field(default_factory=dict)
    # 计算块 {vat: {...}, surcharges: {...}}
    calculation: dict | None = None
    # 复算所需的输入（纳税人身份、金额、含税口径、进项等）
    calculation_context: dict | None = None
    as_of: datetime | None = None


@dataclass
class VerificationReport:
    """一次验证的完整结果。"""

    results: list[VerifierResult] = field(default_factory=list)
    outcome: str = "passed"  # passed / fixed / regenerated / degraded
    retries: int = 0
    note: str = ""

    @property
    def issues(self) -> list[Issue]:
        return [issue for result in self.results for issue in result.issues]

    @property
    def blocking(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity is Severity.BLOCKING]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "passed": self.passed,
            "retries": self.retries,
            "note": self.note,
            "results": [result.to_dict() for result in self.results],
            "issue_codes": sorted({issue.code for issue in self.issues}),
        }

    def explain(self) -> str:
        lines = [f"验证结论：{self.outcome}（重试 {self.retries} 次）"]
        for result in self.results:
            flag = "通过" if result.passed else "未通过"
            lines.append(f"  · {result.name}：{flag}，检查 {result.checked} 项")
            for issue in result.issues:
                lines.append(f"      [{issue.severity.value}] {issue.message}")
        if self.note:
            lines.append(f"  说明：{self.note}")
        return "\n".join(lines)

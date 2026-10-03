"""筹划引擎（技术方案 4.10～4.14）。

**先讲清楚筹划和问答的区别**，否则会按错误的方式建：

| | 普通问答 | 筹划 |
|---|---|---|
| 输入 | 一个问题 | 企业画像（主体/业务/财务/目标/约束） |
| 过程 | 检索 → 生成 | 检索 → 生成候选方案 → 量化测算 → 合法性审查 → 风险分级 |
| 输出 | 一个答案 | 多个方案 + 对比表 + 依据/效果/条件/风险 |
| 最严重的失败 | 答错 | **给出违法方案** |

系统定位（决定了全部设计）：**在合法边界内找最优解，并把这个边界明确告诉用户**。
依法节税是纳税人的权利；违法的是虚构交易、隐瞒收入、无实质经营套取优惠。
两者的分界点落在三处：有没有合理商业目的、有没有真实业务实质、有没有违反强制性规定。
"""

from app.planning.legality import LegalityChecker, LegalityReport
from app.planning.dispatch import DispatchResult, dispatch
from app.planning.profile import CompanyProfile, ProfileError
from app.planning.redlines import RedLineHit, RedLineMatcher
from app.planning.review import (
    ACTION_APPROVE,
    ACTION_APPROVE_WITH_CHANGES,
    ACTION_REJECT,
    ReviewDesk,
    ReviewItem,
)
from app.planning.space import (
    CandidatePlan,
    Measurement,
    PlanningSpaceGenerator,
    build_comparison,
)

__all__ = [
    "ACTION_APPROVE",
    "ACTION_APPROVE_WITH_CHANGES",
    "ACTION_REJECT",
    "CandidatePlan",
    "CompanyProfile",
    "DispatchResult",
    "LegalityChecker",
    "LegalityReport",
    "Measurement",
    "PlanningSpaceGenerator",
    "ProfileError",
    "RedLineHit",
    "RedLineMatcher",
    "ReviewDesk",
    "ReviewItem",
    "build_comparison",
    "dispatch",
]

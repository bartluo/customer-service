"""输出控制与高风险不外泄（技术方案 4.13）。

    🟢 稳健 → 直接给用户，同时抄送专家复核队列（事后抽检）
    🟡 审慎 → 给用户（带提示），同时推送专家复核（限时确认，超时降级标注）
    🔴 高风险 → **只进专家复核台，不出现给用户的响应里**

这条是硬约束，不是"建议"：🔴 方案依赖激进解释、存在被调整风险，
发给用户就等于系统在替一个可能违法的做法背书。**误放一次，后果由用户承担。**

所以这里不返回一个"过滤后的列表"，而是返回**两条互斥的通道**：
user_facing 与 review_queue 在代码层面就分开，调用方拿不到"忘记过滤"的机会。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.planning.profile import CompanyProfile
from app.planning.redlines import RedLineMatcher
from app.planning.space import CandidatePlan


@dataclass
class DispatchResult:
    """一次分流的结果。两条通道互斥且都显式给出。"""

    user_facing: list[CandidatePlan] = field(default_factory=list)
    # 需要专家看的：🔴（从不外发）+ 🟡（已外发但要专家确认口径）
    review_queue: list[CandidatePlan] = field(default_factory=list)
    # 🟡 方案必须带提示给用户（output_with_prompt）
    prompts: list[str] = field(default_factory=list)
    # 事后抽检的 🟢 方案（require_review=False，但仍抄送队列）
    spot_check: list[CandidatePlan] = field(default_factory=list)

    @property
    def withheld(self) -> list[CandidatePlan]:
        """**真正没发给用户**的方案——只有 🔴。

        别把整个 review_queue 当成 withheld：🟡 是"发给用户 + 同时送专家确认"，
        把它们算成"拦截"会让人以为系统少给了方案。
        """

        return [plan for plan in self.review_queue if plan.risk_level == "red"]

    def explain(self) -> str:
        lines = [
            f"给用户：{len(self.user_facing)} 个方案"
            + (f"（其中 {len(self.prompts)} 个带提示）" if self.prompts else ""),
            f"送专家复核：{len(self.review_queue)} 个"
            f"（其中不发给用户的 {len(self.withheld)} 个）",
        ]
        for plan in self.withheld:
            lines.append(f"  ⛔ 已拦截：{plan.name}（{plan.risk_level}）")
        for plan in self.review_queue:
            if plan.risk_level == "red":
                continue
            lines.append(f"  ⏳ 已发用户、待专家确认：{plan.name}（{plan.risk_level}）")
        for prompt in dict.fromkeys(self.prompts):
            lines.append(f"  ⚠ {prompt}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "user_facing": [plan.to_dict() for plan in self.user_facing],
            "review_queue": [plan.to_dict() for plan in self.review_queue],
            "withheld": [plan.code for plan in self.withheld],
            "prompts": list(dict.fromkeys(self.prompts)),
            "spot_check": [plan.code for plan in self.spot_check],
        }


def dispatch(
    plans: list[CandidatePlan],
    *,
    matcher: RedLineMatcher | None = None,
    profile: CompanyProfile | None = None,
) -> DispatchResult:
    """按风险等级把方案分流到"给用户"和"进复核台"两条通道。"""

    matcher = matcher or RedLineMatcher()
    result = DispatchResult()

    for plan in plans:
        config = matcher.risk_level_config(plan.risk_level)
        action = config.get("action")
        if action == "review_only":
            # 🔴：只进复核台。这里绝不放进 user_facing——这是这条设计的全部意义。
            result.review_queue.append(plan)
            continue

        result.user_facing.append(plan)
        if action == "output_with_prompt" and config.get("prompt"):
            result.prompts.append(config["prompt"])
        if config.get("require_review"):
            result.review_queue.append(plan)
        else:
            # 🟢 也要抄送队列做事后抽检：稳健不等于零风险，抽检能发现系统性偏差。
            result.spot_check.append(plan)

    # 给用户的方案里如果还混着 🔴，说明上面逻辑被改坏了——宁可整体不发。
    leaked = [plan for plan in result.user_facing if plan.risk_level == "red"]
    if leaked:
        result.user_facing = [plan for plan in result.user_facing if plan.risk_level != "red"]
        for plan in leaked:
            if plan not in result.review_queue:
                result.review_queue.append(plan)
    return result

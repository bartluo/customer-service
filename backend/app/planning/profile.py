"""企业画像：筹划的结构化输入。

为什么筹划必须先要画像，不能直接问一句答一句：
  筹划的输入是"一家企业的一整套情况"（主体、业务、财务、目标、约束），
  少了任何一块，给出的方案都可能是不可执行的。
  例如不知道"能不能改合同"就推荐结构安排型方案，用户根本落不了地。

设计原则与事实抽取一致：**缺什么就明说，不替用户假设**。
`missing_required()` 返回还缺哪些字段，调用方据此决定追问还是拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


class ProfileError(ValueError):
    """画像本身不合法（取值不在允许范围内等）。"""


# 允许取值：写死在代码里而不是配置里，因为它们是"枚举语义"，
# 配置改错会导致下游匹配全部失准。
ENTITY_TYPES = ("有限公司", "股份有限公司", "合伙企业", "个体工商户", "个人独资企业")
TAXPAYER_TYPES = ("一般纳税人", "小规模纳税人")
TAXPAYER_STATUS = ("盈利", "亏损", "盈亏平衡")
RISK_PREFERENCES = ("保守", "稳健", "进取")
GOALS = (
    "降低税负",  # 常规降负
    "递延纳税",  # 把税往后推
    "合规整改",  # 把不合规的地方改过来
    "特定事项",  # 股权转让 / 并购 / 重组
)

# 约束：能否调整的弹性。落不了地的方案比不给方案更糟——
# 用户会以为"系统推荐了但没人能执行"，从而对整个系统失去信任。
FLEXIBILITY_FIELDS = (
    "can_change_contract",  # 能否调整合同条款
    "can_change_entity",  # 能否调整主体架构
    "can_change_timing",  # 能否调整交易时点
)


@dataclass
class CompanyProfile:
    """一家企业的筹划输入画像。"""

    # 需要按 Decimal 处理的金额字段。接口/命令行/脚本三处都要用同一份清单：
    # 各写一份的结果是"某条路径把金额当字符串算"，那种错很晚才会暴露。
    MONEY_FIELDS = (
        "annual_revenue",
        "revenue",
        "cost",
        "profit",
        "total_assets",
        "taxable_income",
        "historical_loss",
    )

    @classmethod
    def from_mapping(cls, data: dict) -> "CompanyProfile":
        """从普通字典建画像：金额归一到 Decimal，未知字段直接报错。

        未知字段报错而不是忽略：画像字段拼错（例如写成 `annual_income`）
        时静默忽略，等于用户填的收入根本没进模型，方案会基于"收入没填"生成。
        """

        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = [key for key in data if key not in known]
        if unknown:
            raise ProfileError(f"画像里有无法识别的字段：{'、'.join(sorted(unknown))}")

        kwargs: dict = {}
        for key, value in data.items():
            if key in cls.MONEY_FIELDS and value is not None and value != "":
                kwargs[key] = Decimal(str(value))
            else:
                kwargs[key] = value
        return cls(**kwargs)

    # ---- 主体 ----
    entity_type: str | None = None
    taxpayer_type: str | None = None
    industry: str | None = None
    region: str | None = None
    employees: int | None = None
    annual_revenue: Decimal | None = None

    # ---- 业务 ----
    business_model: str | None = None
    transaction_structure: str | None = None
    main_income_source: str | None = None

    # ---- 财务 ----
    revenue: Decimal | None = None
    cost: Decimal | None = None
    profit: Decimal | None = None
    total_assets: Decimal | None = None
    taxable_income: Decimal | None = None
    historical_loss: Decimal | None = None

    # ---- 目标 ----
    goal: str | None = None
    goal_detail: str | None = None

    # ---- 约束 ----
    risk_preference: str | None = None
    flexible: dict[str, bool] = field(default_factory=dict)

    # ---- 反避税检查要用的商业理由（用户自己填，系统不替他编） ----
    business_purpose: str | None = None  # 为什么做这件事（与税无关的理由）
    business_benefit: str | None = None  # 带来什么真实经营效益
    evidence_available: list[str] = field(default_factory=list)  # 能提供哪些证据
    business_authentic: bool | None = None  # 是否确认真实业务实质

    # ---- 校验 ----
    def validate(self) -> list[str]:
        """返回取值错误清单（空列表 = 合法）。"""

        problems: list[str] = []
        for field_name, allowed in (
            ("entity_type", ENTITY_TYPES),
            ("taxpayer_type", TAXPAYER_TYPES),
            ("risk_preference", RISK_PREFERENCES),
            ("goal", GOALS),
        ):
            value = getattr(self, field_name)
            if value is not None and value not in allowed:
                problems.append(f"{field_name} 取值不合法：{value!r}（可选：{'、'.join(allowed)}）")
        if self.employees is not None and self.employees < 0:
            problems.append("employees 不能为负数")
        for field_name in ("revenue", "cost", "profit", "total_assets", "taxable_income"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                problems.append(f"{field_name} 不能为负数")
        return problems

    def missing_required(self) -> list[str]:
        """还缺哪些做筹划必须知道的字段。"""

        missing: list[str] = []
        if not self.entity_type:
            missing.append("组织形式（有限公司 / 合伙企业 / 个体工商户…）")
        if not self.taxpayer_type:
            missing.append("纳税人身份（一般纳税人 / 小规模纳税人）")
        if not self.goal:
            missing.append("筹划目标（降低税负 / 递延纳税 / 合规整改 / 特定事项）")
        if self.risk_preference is None:
            missing.append("风险偏好（保守 / 稳健 / 进取）")
        if not self.flexible:
            missing.append("可调整弹性（能否改合同 / 改主体 / 改时点）")
        if not self.business_authentic:
            missing.append("业务真实性确认（是否有真实业务实质）")
        return missing

    def to_dict(self) -> dict:
        def money(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "entity_type": self.entity_type,
            "taxpayer_type": self.taxpayer_type,
            "industry": self.industry,
            "region": self.region,
            "employees": self.employees,
            "annual_revenue": money(self.annual_revenue),
            "business_model": self.business_model,
            "transaction_structure": self.transaction_structure,
            "main_income_source": self.main_income_source,
            "revenue": money(self.revenue),
            "cost": money(self.cost),
            "profit": money(self.profit),
            "total_assets": money(self.total_assets),
            "taxable_income": money(self.taxable_income),
            "historical_loss": money(self.historical_loss),
            "goal": self.goal,
            "goal_detail": self.goal_detail,
            "risk_preference": self.risk_preference,
            "flexible": dict(self.flexible),
            "business_purpose": self.business_purpose,
            "business_benefit": self.business_benefit,
            "evidence_available": list(self.evidence_available),
            "business_authentic": self.business_authentic,
        }

"""税费计算接口的数据结构。

金额一律走**字符串**：JSON 的数字是双精度浮点，`0.1 + 0.2` 会得到
0.30000000000000004。财税场景里这种误差会一路传到申报表上。
计算引擎内部用 Decimal，接口层就只做搬运，不做转换。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class TaxCalcRequest(BaseModel):
    """一次税费计算。字段都可选，缺什么由引擎判断能不能算。"""

    # —— 增值税 ——
    taxpayer_type: str | None = Field(
        default=None, description="一般纳税人 / 小规模纳税人 / 个体工商户"
    )
    sales_amount: Decimal | None = Field(default=None, description="销售额或含税金额")
    amount_includes_tax: bool = Field(default=False, description="上面的金额是否含税")
    business_type: str | None = Field(default=None, description="业务类型，用于匹配税率")
    rate: Decimal | None = Field(default=None, description="直接指定税率或征收率，如 0.13")
    output_vat: Decimal | None = Field(default=None, description="一般计税：已知销项税额")
    input_vat: Decimal | None = Field(default=None, description="一般计税：可抵扣进项税额")

    # —— 附加税费 ——
    include_surcharges: bool = Field(default=True, description="是否一并计算附加税费")
    consumption_tax_payable: Decimal = Field(default=Decimal("0"), description="实际缴纳的消费税")
    location: str | None = Field(
        default=None, description="城建税适用地区：市区 / 县城、镇 / 其他。不填按最低档并提示"
    )

    as_of: str | None = Field(default=None, description="适用时点 YYYY-MM-DD，默认今天")
    period_scope: str | None = Field(
        default=None,
        description=(
            "数据期间口径：month / quarter。"
            "小规模纳税人的免税额按月销售额 10 万元或季度销售额 30 万元判断，"
            "填了才知道该用哪一档；不填时系统两档都算，结论一致才给结论"
        ),
    )


class CalcStepOut(BaseModel):
    """一个计算步骤：公式 + 代入 + 结果 + 依据。"""

    title: str
    formula: str
    substitution: str
    result: str
    citation: str = ""


class CalcBlockOut(BaseModel):
    """一块计算（增值税或附加税费）。"""

    tax_type: str
    method: str = ""
    payable: str
    steps: list[CalcStepOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    discount_applied: str | None = None
    rule_version: str = ""
    computable: bool = Field(description="false 表示缺参数或口径不确定，payable 不可当结论用")


class TaxCalcResponse(BaseModel):
    """计算结果。"""

    vat: CalcBlockOut
    surcharges: CalcBlockOut | None = None
    total_payable: str = Field(description="增值税 + 附加税费合计（算不出来的块不计入）")
    notes: list[str] = Field(default_factory=list, description="跨块的提示与待确认事项")
    rule_version: str = ""
    disclaimer: str = Field(
        description="计算口径说明。数字有依据，但适用与否仍以主管税务机关认定为准"
    )

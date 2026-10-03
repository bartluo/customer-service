"""税费计算接口。

对应技术方案 12.4：`POST /api/v1/calc/tax`——供独立计算器与客户系统直接调用。

这一层不写任何计算逻辑。数字全部来自计算引擎（规则配置在
`domains/finance_tax/tax_rules.yaml`），本模块只做三件事：
  1. 把请求字段映射成引擎输入；
  2. 需要时把增值税结果接到附加税费上（附加税费的计税依据就是实际缴纳的增值税）；
  3. 把结果整理成前端可直接分步渲染的结构。

一个刻意的约定：**算不出来时返回 200，而不是报错**。
"没给纳税人身份，所以无法确定算法"不是一个系统故障，而是一个需要用户补信息
的正常结果。报 4xx 会让调用方以为接口坏了，把"待确认"丢掉。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends

from app.calculation import get_engine
from app.calculation.engine import SurchargeInput, VatInput
from app.calculation.money import yuan
from app.dependencies import require_permission
from app.domain.permissions import CALC_RUN
from app.schemas.calc import CalcBlockOut, CalcStepOut, TaxCalcRequest, TaxCalcResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/calc", tags=["税费计算"])

DISCLAIMER = (
    "计算过程与适用税率均来自现行有效政策与规则库，可在每步的「依据」处核对原文。"
    "是否适用仍取决于您的实际情形，最终以主管税务机关认定为准。"
)


def _block(result) -> CalcBlockOut:
    """把引擎结果转成接口结构。steps 为空即视为"算不出来"。"""

    return CalcBlockOut(
        tax_type=result.tax_type,
        method=result.method,
        payable=str(result.payable),
        steps=[CalcStepOut(**step.to_dict()) for step in result.steps],
        notes=list(result.notes),
        citations=list(result.citations),
        discount_applied=result.discount_applied,
        rule_version=result.rule_version,
        computable=bool(result.steps),
    )


@router.post(
    "/tax",
    response_model=TaxCalcResponse,
    summary="税费计算（增值税 + 附加税费）",
    dependencies=[Depends(require_permission(CALC_RUN))],
)
def calc_tax(payload: TaxCalcRequest) -> TaxCalcResponse:
    engine = get_engine()

    vat_result = engine.calc_vat(
        VatInput(
            taxpayer_type=payload.taxpayer_type,
            sales_amount=payload.sales_amount,
            amount_includes_tax=payload.amount_includes_tax,
            business_type=payload.business_type,
            rate=payload.rate,
            output_vat=payload.output_vat,
            input_vat=payload.input_vat,
            as_of=payload.as_of,
            period_scope=payload.period_scope,
        )
    )

    surcharge_block: CalcBlockOut | None = None
    notes: list[str] = []

    if payload.include_surcharges:
        if not vat_result.steps:
            # 增值税算不出来就没有"实际缴纳的增值税"，附加税费的计税依据不存在。
            # 这里明确说明原因，而不是给一个 0。
            notes.append("增值税未能算出结果，因此没有计算附加税费（附加税费以实际缴纳的增值税为计税依据）。")
        else:
            surcharge_result = engine.calc_surcharges(
                SurchargeInput(
                    vat_payable=vat_result.payable,
                    consumption_tax_payable=payload.consumption_tax_payable,
                    location=payload.location,
                    taxpayer_type=payload.taxpayer_type,
                    as_of=payload.as_of,
                )
            )
            surcharge_block = _block(surcharge_result)
            if vat_result.payable == 0:
                notes.append(
                    "增值税应纳税额为 0，附加税费计税依据为 0；"
                    "但请注意：免征增值税不等于免征附加税费的情形，需按当地口径确认。"
                )

    total = Decimal("0.00")
    if vat_result.steps:
        total += yuan(vat_result.payable)
    if surcharge_block and surcharge_block.computable:
        total += yuan(surcharge_block.payable)

    return TaxCalcResponse(
        vat=_block(vat_result),
        surcharges=surcharge_block,
        total_payable=str(total),
        notes=notes,
        rule_version=vat_result.rule_version,
        disclaimer=DISCLAIMER,
    )

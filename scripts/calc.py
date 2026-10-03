"""税费计算命令行。

用法（项目根目录）：
    python scripts/calc.py vat --taxpayer 一般纳税人 --amount 1000000 ^
        --business 销售货物 --input-vat 30000
    python scripts/calc.py vat --taxpayer 小规模纳税人 --amount 1030000 --includes-tax
    python scripts/calc.py surcharge --vat 100000 --location 市区 --taxpayer 小规模纳税人

为什么要有命令行入口：
  独立计算器与问答主链路用的是同一套引擎。
  先有命令行，才能在接入界面之前把算得对不对验清楚。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description="税费计算（增值税 / 附加税费）")
    sub = parser.add_subparsers(dest="command", required=True)

    vat = sub.add_parser("vat", help="计算增值税")
    vat.add_argument("--taxpayer", help="纳税人身份：一般纳税人 / 小规模纳税人")
    vat.add_argument("--amount", help="销售额或含税金额")
    vat.add_argument("--includes-tax", action="store_true", help="上面的金额是含税的")
    vat.add_argument("--business", help="业务类型，用于匹配税率（如 销售货物 / 交通运输服务）")
    vat.add_argument("--rate", help="直接指定税率，如 0.13 或 13%%")
    vat.add_argument("--output-vat", help="已知销项税额（一般计税）")
    vat.add_argument("--input-vat", help="已知可抵扣进项税额（一般计税）")
    vat.add_argument("--as-of", help="适用时点 YYYY-MM-DD，默认今天")

    sur = sub.add_parser("surcharge", help="计算附加税费")
    sur.add_argument("--vat", required=True, help="实际缴纳的增值税")
    sur.add_argument("--consumption-tax", default="0", help="实际缴纳的消费税，默认 0")
    sur.add_argument("--location", help="纳税人所在地：市区 / 县城、镇 / 其他")
    sur.add_argument("--taxpayer", help="纳税人身份，用于判断是否适用减免")
    sur.add_argument("--as-of", help="适用时点 YYYY-MM-DD，默认今天")

    args = parser.parse_args()

    from app.calculation import get_engine
    from app.calculation.engine import SurchargeInput, VatInput

    engine = get_engine()
    if args.command == "vat":
        result = engine.calc_vat(
            VatInput(
                taxpayer_type=args.taxpayer,
                sales_amount=args.amount,
                amount_includes_tax=args.includes_tax,
                business_type=args.business,
                rate=args.rate,
                output_vat=args.output_vat,
                input_vat=args.input_vat,
                as_of=args.as_of,
            )
        )
    else:
        result = engine.calc_surcharges(
            SurchargeInput(
                vat_payable=args.vat,
                consumption_tax_payable=args.consumption_tax,
                location=args.location,
                taxpayer_type=args.taxpayer,
                as_of=args.as_of,
            )
        )

    print(result.explain())
    # 算不出来（有提示且没有步骤）时返回非零，便于脚本化调用时发现问题
    return 0 if result.steps else 2


if __name__ == "__main__":
    raise SystemExit(main())

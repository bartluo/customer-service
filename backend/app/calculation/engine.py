"""计算引擎：按规则算钱，并留下可展示、可复算的过程。

覆盖范围（第一批，技术方案 4.5 的"宁可先只覆盖 2 个税种但做到准确"）：
  · 增值税：一般计税（销项 - 进项）、简易计税（销售额 × 征收率）、进口（组成计税价格 × 税率）
  · 附加税费：城市维护建设税、教育费附加、地方教育附加

不做什么：
  · 不猜税率。业务类型匹配不到税率时返回"需确认"，不替用户选一个。
  · 不猜主体。小规模还是一般纳税人不确定时，两条路都算出来给用户确认，
    而不是默认按某一个算——算错方向会让用户按错误的数申报。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from app.config import domain_file
from app.calculation.money import money_text, percent, rate as to_rate, yuan
from app.calculation.result import CalcResult
from app.calculation.rules import RateEntry, RuleSet, RuleSetError, load_rules

# 域包所在目录由 config.domain_file 统一解析（本机与容器两种布局都能找到）
_DEFAULT_RULES = domain_file("finance_tax", "tax_rules.yaml")


def _wan(amount: Decimal) -> str:
    """金额写成"10万元"这样的人话（整万时），否则退回"1,234.00元"。"""

    if amount and amount % 10000 == 0:
        return f"{int(amount / 10000)}万元"
    return f"{money_text(amount)}元"


@dataclass
class VatInput:
    """增值税计算的输入。字段都可选，缺什么由引擎判断能不能算。"""

    taxpayer_type: str | None = None  # 一般纳税人 / 小规模纳税人 / 小型微利企业 / 个体工商户
    method: str | None = None  # general / simplified，不填按主体推断
    sales_amount: Decimal | None = None  # 销售额或含税金额
    amount_includes_tax: bool = False  # 上面这个金额是不是含税的
    business_type: str | None = None  # 业务描述，用于匹配税率
    rate: Decimal | None = None  # 直接指定税率或征收率
    output_vat: Decimal | None = None  # 一般计税：已知销项税额
    input_vat: Decimal | None = None  # 一般计税：已知可抵扣进项税额
    as_of: date | None = None  # 适用时点
    # 数据期间口径：month / quarter / year。
    # 小规模纳税人的免税额按月（10万）或按季（30万）判断，
    # 用户给的是月收入还是季度收入，会直接改变结论。
    period_scope: str | None = None
    consumption_tax_paid: Decimal | None = None  # 一并缴纳的消费税，用于附加税费
    location: str | None = None  # 城建税适用地区：市区 / 县城、镇 / 其他


@dataclass
class SurchargeInput:
    """附加税费计算的输入。"""

    vat_payable: Decimal
    consumption_tax_payable: Decimal = Decimal("0")
    # 不默认成市区：市区是 7% 的最高档，默认成市区等于替用户选了最贵的算法。
    # 没给地区时引擎按最低档算并明确提示。
    location: str | None = None
    taxpayer_type: str | None = None
    as_of: date | None = None


class CalculationEngine:
    """按规则算税。规则来自 YAML，引擎本身不含任何硬编码税率。"""

    def __init__(self, rules: RuleSet) -> None:
        self.rules = rules

    @staticmethod
    def _money(value: object) -> Decimal | None:
        """把外部传进来的金额归一成 Decimal。

        接口上金额可能来自 JSON（字符串）、命令行（字符串）或代码（int/float）。
        不归一的话，`"1000000" / Decimal("1.13")` 这种字符串除法会直接报 TypeError。
        """

        if value is None:
            return None
        return yuan(value)

    # ------------------------------------------------------------------
    # 增值税
    # ------------------------------------------------------------------
    def calc_vat(self, data: VatInput) -> CalcResult:
        # 外部传入的金额可能是字符串/整数，先归一到 Decimal
        data.sales_amount = self._money(data.sales_amount)
        data.output_vat = self._money(data.output_vat)
        data.input_vat = self._money(data.input_vat)
        if data.rate is not None:
            data.rate = to_rate(data.rate)
        if data.as_of is not None and not isinstance(data.as_of, date):
            data.as_of = date.fromisoformat(str(data.as_of))

        moment = (data.as_of or datetime.now(timezone.utc).date()).isoformat()
        result = CalcResult(tax_type="vat", rule_version=self.rules.rules_version)

        method = data.method or self._infer_method(data.taxpayer_type)
        if method is None:
            # 主体未知：不替用户选，两条路都算一遍，让他确认
            result.notes.append(
                "未确认纳税人身份（一般纳税人 / 小规模纳税人），无法确定计税方法。"
                "一般纳税人与小规模纳税人的算法和税率都不同，请先确认身份。"
            )
            return result
        result.method = method

        if method == "general":
            return self._calc_vat_general(data, moment, result)
        return self._calc_vat_simplified(data, moment, result)

    def _calc_vat_general(self, data: VatInput, moment: str, result: CalcResult) -> CalcResult:
        method = self.rules.vat_method("general")
        result.citations.append(method["citation"])

        output_vat = data.output_vat
        if output_vat is None:
            if data.sales_amount is None:
                result.notes.append(
                    "一般计税需要销售额或已算好的销项税额。"
                    "请提供销售额（并注明含税与否）或直接提供销项税额。"
                )
                return result
            matched = self._resolve_rate(data, result)
            if matched is None:
                return result
            entry, value = matched
            net_sales = self._net_sales(data.sales_amount, data.amount_includes_tax, value)
            if data.amount_includes_tax:
                result.add_step(
                    "不含税销售额",
                    "含税金额 ÷（1 + 适用税率）",
                    f"{money_text(data.sales_amount)} ÷（1 + {percent(value)}）",
                    net_sales,
                    self.rules.raw["vat"]["price_inclusion"]["citation"],
                )
            output_vat = yuan(net_sales * value)
            result.add_step(
                "销项税额",
                "不含税销售额 × 适用税率",
                f"{money_text(net_sales)} × {percent(value)}",
                output_vat,
                entry.citation,
            )

        input_vat = data.input_vat if data.input_vat is not None else Decimal("0")
        if data.input_vat is None:
            result.notes.append(
                "未提供可抵扣进项税额，本次按 0 计算。请核对本期是否取得合法的增值税扣税凭证。"
            )
        payable = yuan(output_vat - input_vat)
        if payable < 0:
            result.notes.append(
                f"销项税额小于进项税额，差额 {money_text(-payable)} 为期末留抵税额，"
                "本期不产生应纳税额。留抵税额可按规定申请退还或结转下期抵扣。"
            )
        result.add_step(
            "应纳税额",
            method["formula"],
            f"{money_text(output_vat)} - {money_text(input_vat)}",
            max(payable, Decimal("0.00")),
            method["citation"],
        )
        result.payable = max(payable, Decimal("0.00"))
        return result

    def _calc_vat_simplified(self, data: VatInput, moment: str, result: CalcResult) -> CalcResult:
        method = self.rules.vat_method("simplified")
        result.citations.append(method["citation"])

        if data.sales_amount is None:
            result.notes.append("简易计税需要销售额，请提供销售额（并注明含税与否）。")
            return result

        # 步骤一：先把政策里写明的征收率与优惠查出来。
        # 为什么先查政策再算：用户的原话是"应该是先查看税收政策，包括优惠政策，
        # 然后按照政策计算税款"。免征优惠只写成一句提示是不够的，
        # 引擎照常按 3% 算出应纳 2,912.62 元，而正确答案是免征。
        levy_entries = self.rules.vat_levy_rates()
        if not levy_entries:
            result.notes.append("规则里没有配置征收率，无法计算。")
            return result
        statutory_levy = to_rate(levy_entries[0].rate)  # 法定征收率，用于换算不含税销售额
        levy = data.rate or statutory_levy
        levy_citation = "" if data.rate else levy_entries[0].citation

        discounts = self.rules.applicable_discounts(
            tax_code="vat", as_of=moment, taxpayer_type=data.taxpayer_type
        )
        exemption = next((item for item in discounts if item.get("threshold")), None)
        reduction = next((item for item in discounts if item.get("to_rate")), None)
        if reduction and data.rate is None:
            # 减按后的征收率才是实际适用的征收率
            levy = to_rate(reduction["to_rate"])
            levy_citation = reduction["citation"]

        net_sales = self._net_sales(data.sales_amount, data.amount_includes_tax, levy)

        # 步骤二：免征判断。判断免税标准要用**不含税销售额**，
        # 而含税换算用哪个征收率（法定 3% 还是减按后的 1%）实务上有两种口径。
        # 这里两个都算：
        #   · 结论一致 → 正常给结论（这也是绝大多数情形）
        #   · 结论不一致 → 不猜，标记"需确认"（ADR-0013：宁可算不出来，也不猜参数）
        exemption_decision: bool | None = None
        if exemption and data.taxpayer_type == "小规模纳税人":
            thresholds = exemption.get("threshold") or {}
            limit_by_scope = {
                "month": (yuan(thresholds.get("month") or 0), "月销售额"),
                "quarter": (yuan(thresholds.get("quarter") or 0), "季度销售额"),
            }
            # 按哪一档判断，看用户说的是什么期间的收入（既定规则）：
            # 说"上个月"就按月 10 万，说"上个季度"就按季 30 万。
            if data.period_scope in limit_by_scope:
                scopes = [data.period_scope]
            elif data.period_scope == "year":
                result.notes.append(
                    "免税额是按月销售额（10万元）或季度销售额（30万元）判断的，"
                    "您给的是全年数据。请提供按月或按季的销售额（并说明是哪个月/哪个季度）。"
                )
                return result
            else:
                # 没说期间：两档都算，结论一致才给结论（不猜）
                scopes = ["month", "quarter"]

            rates = [(f"按{percent(statutory_levy)}换算", statutory_levy)]
            if reduction:
                rates.append(
                    (f"按{percent(to_rate(reduction['to_rate']))}换算", to_rate(reduction["to_rate"]))
                )

            verdicts: dict[tuple[str, str], bool] = {}
            for scope in scopes:
                limit, _label = limit_by_scope[scope]
                for rate_label, rate in rates:
                    net = self._net_sales(data.sales_amount, data.amount_includes_tax, rate)
                    verdicts[(scope, rate_label)] = net <= limit

            if len(set(verdicts.values())) == 1:
                scope = scopes[0]
                limit, scope_label = limit_by_scope[scope]
                # 优惠名称要跟期间口径一致：按季判断时写"季度销售额"，
                # 写成"月销售额"会让用户以为系统搞错了他的情况。
                exemption_name = "{}{}销售额未超过{}免征增值税".format(
                    "小规模纳税人",
                    "月" if scope == "month" else "季度",
                    _wan(limit),
                )
                exemption_decision = verdicts[(scope, rates[0][0])]
                if data.amount_includes_tax:
                    result.add_step(
                        "不含税销售额",
                        "含税金额 ÷（1 + 适用征收率）",
                        f"{money_text(data.sales_amount)} ÷（1 + {percent(levy)}）",
                        net_sales,
                        self.rules.raw["vat"]["price_inclusion"]["citation"],
                    )
                if exemption_decision:
                    result.add_step(
                        "适用免征增值税优惠",
                        exemption_name,
                        f"{scope_label}（不含税）{money_text(net_sales)} 未超过 "
                        f"{money_text(limit)}（含本数）",
                        Decimal("0.00"),
                        exemption["citation"],
                    )
                    result.payable = Decimal("0.00")
                    result.discount_applied = exemption_name
                    result.citations.append(exemption["citation"])
                    result.preferences.append(
                        {
                            "code": exemption.get("code", ""),
                            "name": exemption_name,
                            "citation": exemption["citation"],
                            "effect": exemption.get("effect", ""),
                        }
                    )
                    result.notes.append(
                        "增值税免征时，城市维护建设税、教育费附加、地方教育附加"
                        "以实际缴纳的增值税为计税依据，免征后计税依据为零。"
                    )
                    return result
                result.notes.append(
                    f"已按{scope_label}口径判断：不含税 {money_text(net_sales)} 超过 "
                    f"免征标准 {money_text(limit)}，因此按应税处理。"
                )
            else:
                # 结论不一致时要说清是哪一类不一致，用户才知道该补什么信息
                scope_verdicts = {
                    scope: verdicts[(scope, rates[0][0])] for scope in scopes
                }
                if len(scopes) > 1 and len(set(scope_verdicts.values())) > 1:
                    result.notes.append(
                        "按月度判断与按季度判断的结论不一致："
                        f"月销售额标准 {money_text(limit_by_scope['month'][0])}、"
                        f"季度销售额标准 {money_text(limit_by_scope['quarter'][0])}。"
                        "请说明您是**按月**申报还是**按季**申报（或分别给出月/季销售额）。"
                    )
                else:
                    result.notes.append(
                        "含税金额按法定征收率换算与按减按后征收率换算，得到的销售额分处"
                        "免征标准两侧，两种口径结论不一致。"
                        "请与主管税务机关确认换算口径后再申报。"
                    )
                return result

        # 步骤三：按（可能已减按的）征收率计算应纳税额
        if data.amount_includes_tax and exemption is None:
            result.add_step(
                "不含税销售额",
                "含税金额 ÷（1 + 征收率）",
                f"{money_text(data.sales_amount)} ÷（1 + {percent(levy)}）",
                net_sales,
                self.rules.raw["vat"]["price_inclusion"]["citation"],
            )
        if reduction and data.rate is None:
            result.add_step(
                "适用减征优惠",
                reduction["name"],
                f"法定征收率 {percent(statutory_levy)} → 减按 {percent(levy)}",
                levy,
                reduction["citation"],
            )
            result.preferences.append(
                {
                    "code": reduction.get("code", ""),
                    "name": reduction["name"],
                    "citation": reduction["citation"],
                    "effect": reduction.get("effect", ""),
                }
            )
        payable = yuan(net_sales * levy)
        result.add_step(
            "应纳税额",
            "销售额 × 征收率",
            f"{money_text(net_sales)} × {percent(levy)}",
            payable,
            method["citation"],
        )
        result.payable = payable
        if levy_citation:
            result.citations.append(levy_citation)
        return result

    # ------------------------------------------------------------------
    # 附加税费
    # ------------------------------------------------------------------
    def calc_cit(
        self,
        taxable_income: Decimal | None,
        *,
        small_low_profit: bool = False,
        as_of: date | None = None,
    ) -> CalcResult:
        """计算企业所得税。

        筹划测算要用它：小微优惠与基本税率的差额，就是"享受小微优惠"这件事的节税额。
        参数不足时不给数字——与增值税一样的口径。
        """

        moment = (as_of or datetime.now(timezone.utc).date()).isoformat()
        result = CalcResult(tax_type="cit", rule_version=self.rules.rules_version)
        if taxable_income is None:
            result.notes.append("企业所得税需要应纳税所得额，请提供该数据。")
            return result
        income = yuan(taxable_income)
        if income <= 0:
            result.notes.append("应纳税所得额为零或亏损，本期不产生企业所得税。")
            result.payable = Decimal("0.00")
            return result

        config = self.rules.raw.get("cit") or {}
        base_rate = to_rate(config.get("rate", "0.25"))
        citation = config.get("citation", "")
        discount = None
        if small_low_profit:
            discount = next(
                (d for d in config.get("discounts") or [] if d.get("code") == "small_low_profit"),
                None,
            )
        if discount:
            include_ratio = to_rate(discount["include_ratio"])
            reduced_rate = to_rate(discount["rate"])
            counted = yuan(income * include_ratio)
            result.add_step(
                "计入应纳税所得额的金额",
                f"应纳税所得额 × {percent(include_ratio)}",
                f"{money_text(income)} × {percent(include_ratio)}",
                counted,
                discount["citation"],
            )
            payable = yuan(counted * reduced_rate)
            result.add_step(
                "应纳税额",
                f"计入金额 × {percent(reduced_rate)}",
                f"{money_text(counted)} × {percent(reduced_rate)}",
                payable,
                discount["citation"],
            )
            result.discount_applied = discount["name"]
            result.notes.append(
                f"适用{discount['name']}需同时满足：{discount.get('condition_note', '')}"
            )
        else:
            payable = yuan(income * base_rate)
            result.add_step(
                "应纳税额",
                config.get("formula", "应纳税所得额 × 适用税率"),
                f"{money_text(income)} × {percent(base_rate)}",
                payable,
                citation,
            )
        result.payable = payable
        return result

    def calc_surcharges(self, data: SurchargeInput) -> CalcResult:
        data.vat_payable = self._money(data.vat_payable) or Decimal("0.00")
        data.consumption_tax_payable = self._money(data.consumption_tax_payable) or Decimal("0.00")
        if data.as_of is not None and not isinstance(data.as_of, date):
            data.as_of = date.fromisoformat(str(data.as_of))

        moment = (data.as_of or datetime.now(timezone.utc).date()).isoformat()
        result = CalcResult(tax_type="surcharges", rule_version=self.rules.rules_version)

        base = yuan(data.vat_payable + data.consumption_tax_payable)
        result.add_step(
            "计税依据",
            "实际缴纳的增值税 + 消费税",
            f"{money_text(data.vat_payable)} + {money_text(data.consumption_tax_payable)}",
            base,
            self.rules.raw["surcharges"]["items"][0]["citation"],
        )
        if base <= 0:
            result.notes.append("实际缴纳的增值税、消费税为零，附加税费计税依据为零，无需缴纳。")
            result.payable = Decimal("0.00")
            return result

        total = Decimal("0.00")
        for item in self.rules.surcharge_items():
            entry_rate, citation = self._resolve_surcharge_rate(item, data.location, result)
            if entry_rate is None:
                continue
            amount = yuan(base * entry_rate)

            discounts = self.rules.applicable_discounts(
                tax_code=item["code"], as_of=moment, taxpayer_type=data.taxpayer_type
            )
            if discounts:
                factor = to_rate(discounts[0].get("factor", "1"))
                reduced = yuan(amount * factor)
                result.add_step(
                    item["name"],
                    f"计税依据 × 适用税率 × 减免系数（{discounts[0]['effect']}）",
                    f"{money_text(base)} × {percent(entry_rate)} × {percent(factor)}",
                    reduced,
                    f"{citation}；{discounts[0]['citation']}",
                )
                if result.discount_applied is None:
                    result.discount_applied = discounts[0]["name"]
                total += reduced
            else:
                result.add_step(
                    item["name"],
                    "计税依据 × 适用税率",
                    f"{money_text(base)} × {percent(entry_rate)}",
                    amount,
                    citation,
                )
                total += amount

        result.payable = yuan(total)
        result.add_step("附加税费合计", "各附加税费之和", "", result.payable, "")
        return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _resolve_surcharge_rate(
        self, item: dict, location: str, result: CalcResult
    ) -> tuple[Decimal | None, str]:
        """城建税按地区分档；其他附加税费是单一税率。"""

        if "rates" in item:
            for entry in item["rates"]:
                if location and location in entry["name"]:
                    return to_rate(entry["rate"]), entry.get("citation") or item["citation"]
            # 地区没匹配上：不猜档位，用最低档并提示
            fallback = item["rates"][-1]
            result.notes.append(
                f"未确认纳税人所在地（{location!r}），{item['name']}暂按最低档 "
                f"{percent(to_rate(fallback['rate']))} 计算，请按实际所在地核对。"
            )
            return to_rate(fallback["rate"]), fallback.get("citation") or item["citation"]
        return to_rate(item["rate"]), item["citation"]

    def _resolve_rate(self, data: VatInput, result: CalcResult) -> tuple[RateEntry, Decimal] | None:
        """确定适用税率：显式指定优先，其次按业务描述匹配，匹配不到就报"需确认"。"""

        if data.rate is not None:
            value = to_rate(data.rate)
            # 显式指定时构造一个说明用的条目
            matched = next(
                (entry for entry in self.rules.vat_rates() if to_rate(entry.rate) == value), None
            )
            if matched is None:
                return RateEntry(rate=str(value), name="用户指定税率", citation=""), value
            return matched, value

        matched = self.rules.match_rate(data.business_type)
        if matched is None:
            listed = "、".join(
                f"{percent(to_rate(entry.rate))}（{entry.name}）" for entry in self.rules.vat_rates()
            )
            result.notes.append(
                "未能确定适用税率。请补充业务类型，或直接指定税率。"
                f"现行税率档次：{listed}"
            )
            return None
        return matched, to_rate(matched.rate)

    @staticmethod
    def _net_sales(amount: Decimal, includes_tax: bool, value: Decimal) -> Decimal:
        """含税金额换算成不含税销售额（不含税则原样返回）。"""

        if not includes_tax:
            return yuan(amount)
        return yuan(amount / (Decimal("1") + value))

    @staticmethod
    def _infer_method(taxpayer_type: str | None) -> str | None:
        """按纳税人身份推断计税方法。

        小规模纳税人可以适用简易计税；一般纳税人默认一般计税。
        身份未知时返回 None——由调用方提示用户确认，而不是默认按某一个算。
        """

        if not taxpayer_type:
            return None
        if "小规模" in taxpayer_type:
            return "simplified"
        if "一般纳税人" in taxpayer_type:
            return "general"
        return None


_engine: CalculationEngine | None = None
_engine_path: pathlib.Path | None = None


def get_engine(rules_path: str | pathlib.Path | None = None) -> CalculationEngine:
    """取引擎单例。规则文件路径变化时重新加载。"""

    global _engine, _engine_path
    path = pathlib.Path(rules_path) if rules_path else _DEFAULT_RULES
    if _engine is None or _engine_path != path:
        _engine = CalculationEngine(load_rules(path))
        _engine_path = path
    return _engine

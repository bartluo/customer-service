"""金额计算的小工具。

三条规矩（技术方案 4.5 的"计算引擎要求"）：
  1. 用 Decimal，不用 float。`0.1 + 0.2 != 0.3` 这种误差在财税场景不可接受。
  2. 金额一律量化到"分"（两位小数），四舍五入用 ROUND_HALF_UP——
     税务实务里的"四舍五入"就是这个口径，不是银行家舍入。
  3. 税率用 Decimal 表示（"13%" → Decimal("0.13")），避免二进制浮点误差。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def to_decimal(value: object) -> Decimal:
    """把各种输入转成 Decimal。

    float 要先转成字符串再转 Decimal——Decimal(0.1) 会带出二进制误差，
    Decimal("0.1") 才是干净的。
    """

    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value.strip().replace(",", "").replace("，", ""))
    return Decimal(str(value))


def yuan(value: object) -> Decimal:
    """量化到分（两位小数，四舍五入）。"""

    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def rate(value: object) -> Decimal:
    """把税率转成 Decimal。支持 "13%"/"0.13"/13 三种写法。"""

    if isinstance(value, str) and value.strip().endswith("%"):
        return to_decimal(value.strip().rstrip("%")) / Decimal("100")
    number = to_decimal(value)
    # 大于 1 的多半是"百分之几"的整数写法（13 → 13%）
    return number / Decimal("100") if number > 1 else number


def percent(value: Decimal) -> str:
    """把 Decimal 税率显示成百分比文本（0.13 → 13%）。"""

    # 不能用 normalize()：它把 0.5 显示成 5E+1（科学计数法），
    # 用户看到"5E+1%"根本不知道是 50%。这里手工去掉尾随的 0。
    text = f"{value * Decimal('100'):.10f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def money_text(value: Decimal) -> str:
    """金额加千分位，便于阅读（1000000.00 → 1,000,000.00）。"""

    return f"{yuan(value):,.2f}"

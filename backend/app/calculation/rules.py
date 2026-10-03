"""计算规则加载与校验。

规则文件：domains/finance_tax/tax_rules.yaml（技术方案 4.5 的"规则配置化"）。

为什么要严格校验：规则是配置，写错不会报错、只会算错——
比如税率写成 "13" 被当成 13 倍、公式键名拼错导致整条规则被跳过。
这些都属于"静默出错"，比直接崩掉危险得多。所以加载时就检查必填字段与取值范围。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import yaml

from app.calculation.money import rate as to_rate

# 税率合理区间：0 ~ 1（含）。写错一位就是算错一位，加载期就拦住。
_RATE_MIN = to_rate("0")
_RATE_MAX = to_rate("1")


class RuleSetError(ValueError):
    """规则文件本身有问题（缺字段、税率越界等）。"""


@dataclass(frozen=True)
class RateEntry:
    rate: str
    name: str
    citation: str = ""
    keywords: tuple[str, ...] = ()


@dataclass
class RuleSet:
    """一份完整规则。"""

    domain_id: str
    rules_version: str
    as_of: str
    raw: dict = field(default_factory=dict)

    # ---- 增值税 ----
    def vat_rates(self) -> tuple[RateEntry, ...]:
        return self._entries(self.raw["vat"]["rates"])

    def vat_levy_rates(self) -> tuple[RateEntry, ...]:
        return self._entries(self.raw["vat"]["levy_rates"])

    def vat_method(self, method: str) -> dict:
        methods = self.raw["vat"]["methods"]
        if method not in methods:
            raise RuleSetError(f"未知的计税方法：{method}（可选：{sorted(methods)}）")
        return methods[method]

    def match_rate(self, business_type: str | None) -> RateEntry | None:
        """按业务描述匹配税率。匹配不到返回 None，由调用方决定是追问还是报错。

        为什么不给默认税率：财税上"猜一个税率"比"报错"危险得多——
        用户看到 13% 算出来的数，不会知道系统其实是猜的。

        匹配规则：**取命中关键词最长的那个档次**。
        按配置顺序取第一个会出错——"出口货物"同时命中 13% 档的"货物"
        和 0% 档的"出口货物"，顺序取先就会把出口按 13% 算。
        """

        if not business_type:
            return None
        text = business_type.strip()
        best: tuple[int, RateEntry] | None = None
        for entry in self.vat_rates():
            for keyword in entry.keywords:
                if keyword in text:
                    if best is None or len(keyword) > best[0]:
                        best = (len(keyword), entry)
                    break
        return best[1] if best else None

    # ---- 附加税费 ----
    def surcharge_item(self, code: str) -> dict:
        for item in self.raw["surcharges"]["items"]:
            if item.get("code") == code:
                return item
        raise RuleSetError(f"未知的附加税费：{code}")

    def surcharge_items(self) -> list[dict]:
        return list(self.raw["surcharges"]["items"])

    # ---- 优惠 ----
    def discounts(self) -> list[dict]:
        return list(self.raw.get("discounts") or [])

    def applicable_discounts(
        self,
        *,
        tax_code: str,
        as_of: str,
        taxpayer_type: str | None = None,
    ) -> list[dict]:
        """筛出在给定时点、给定主体下可用的优惠规则。"""

        found: list[dict] = []
        for item in self.discounts():
            if tax_code not in (item.get("applies_to_tax") or []):
                continue
            period = item.get("period") or []
            if len(period) == 2 and not (period[0] <= as_of <= period[1]):
                continue  # 不在有效期内，直接不适用（规则带有效期）
            allowed = (item.get("condition") or {}).get("taxpayer_type")
            if allowed and taxpayer_type and taxpayer_type not in allowed:
                continue
            if allowed and not taxpayer_type:
                continue  # 主体未知时不算数，避免把优惠算给不该享受的人
            found.append(item)
        return found

    @staticmethod
    def _entries(items: list[dict]) -> tuple[RateEntry, ...]:
        entries = []
        for item in items:
            entries.append(
                RateEntry(
                    rate=str(item["rate"]),
                    name=item.get("name", ""),
                    citation=item.get("citation", ""),
                    keywords=tuple(item.get("keywords") or ()),
                )
            )
        return tuple(entries)


def _require(data: dict, key: str, label: str) -> object:
    if key not in data:
        raise RuleSetError(f"{label} 缺少必填字段：{key}")
    return data[key]


def _config_rate(value: object, where: str) -> Decimal:
    """校验配置里的税率写法。

    规则文件必须写得没有歧义：**只接受 "0.13" 或 "13%" 两种写法**。
    裸写 "13" 一律报错——它既可以理解成 13%，也可能是 13 倍，
    而这种歧义在税率上就是算错 100 倍，而且不会报错、只会在用户看到结果时才发现。

    （用户输入走的是另一套：`money.rate()` 对裸数字按百分数处理，
      因为用户说"13 个点"就是 13%。配置比输入严，是刻意的。）
    """

    text = str(value or "").strip()
    if text.endswith("%"):
        return to_rate(text)
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise RuleSetError(f"{where} 的税率无法解析：{value!r}") from exc
    if number > 1:
        raise RuleSetError(
            f"{where} 的税率写法有歧义：{value!r}。请写成 0.13 或 13% 这种明确形式"
        )
    return number


def load_rules(path: str | pathlib.Path) -> RuleSet:
    """读规则文件并做结构校验。"""

    file_path = pathlib.Path(path)
    if not file_path.exists():
        raise RuleSetError(f"规则文件不存在：{file_path}")
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RuleSetError("规则文件顶层必须是对象")

    domain_id = str(_require(data, "domain_id", "规则文件"))
    rules_version = str(_require(data, "rules_version", "规则文件"))
    as_of = str(_require(data, "as_of", "规则文件"))

    vat = _require(data, "vat", "规则文件")
    if not isinstance(vat, dict):
        raise RuleSetError("vat 必须是对象")
    for key in ("methods", "rates", "levy_rates"):
        if key not in vat:
            raise RuleSetError(f"vat 缺少必填字段：{key}")
    for method in ("general", "simplified"):
        if method not in vat["methods"]:
            raise RuleSetError(f"vat.methods 缺少计税方法：{method}")

    # 税率取值检查：写错一位就是算错一位，必须在加载期拦住
    for entry in list(vat["rates"]) + list(vat["levy_rates"]):
        value = _config_rate(entry.get("rate"), "vat.rates")
        if not (_RATE_MIN <= value <= _RATE_MAX):
            raise RuleSetError(f"税率超出合理范围：{entry!r}")
        if not entry.get("citation"):
            raise RuleSetError(f"税率条目必须写明依据条款：{entry!r}")

    surcharges = _require(data, "surcharges", "规则文件")
    if not isinstance(surcharges, dict) or not surcharges.get("items"):
        raise RuleSetError("surcharges.items 不能为空")
    for item in surcharges["items"]:
        if not item.get("code") or not item.get("name"):
            raise RuleSetError(f"附加税费条目缺少 code 或 name：{item!r}")
        if not item.get("citation"):
            raise RuleSetError(f"附加税费条目必须写明依据条款：{item!r}")
        item_rates = item.get("rates") or ([{"rate": item["rate"]}] if item.get("rate") else [])
        if not item_rates:
            raise RuleSetError(f"附加税费条目没有税率：{item!r}")
        for entry in item_rates:
            value = _config_rate(entry.get("rate"), f"surcharges.{item.get('code')}")
            if not (_RATE_MIN <= value <= _RATE_MAX):
                raise RuleSetError(f"附加税费税率超出合理范围：{entry!r}")

    for discount in data.get("discounts") or []:
        if not discount.get("citation"):
            raise RuleSetError(f"优惠规则必须写明依据：{discount!r}")
        period = discount.get("period") or []
        if len(period) != 2:
            raise RuleSetError(f"优惠规则必须写明有效期区间：{discount!r}")

    return RuleSet(domain_id=domain_id, rules_version=rules_version, as_of=as_of, raw=data)

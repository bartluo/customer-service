"""红线检查：命中即拒绝，但必须给替代路径。

红线清单来自 domains/finance_tax/risk_rules.yaml（域包只能加严不能放宽）。

**拒绝的表达方式比拒绝本身更重要**（技术方案 4.12）：
不是冷冰冰地说"不能回答"，而是
    "这个做法属于 X 类违法风险 → 风险在于 Y → 如果您的目的是 Z，合规的做法是……"
用户来找筹划，本质是想要合法的省钱办法；给出替代路径才是专业服务。

当前是纯规则匹配（关键词 + 正则）。规格里写的是"规则匹配 + LLM 判定"，
LLM 那一半留作扩展点：模型更适合识别**换了说法**的同类诉求，
但红线判断事关法律责任，规则表这一层不能省。
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

import yaml

from app.config import domain_file

_DEFAULT_RISK_RULES = domain_file("finance_tax", "risk_rules.yaml")

# 每条红线的识别模式与替代路径。
# 模式只覆盖**明确表达违法意图**的说法；模糊表述交给反避税检查去提示，
# 不要一有风吹草动就拒绝——那会把正常咨询也挡在门外。
_RED_LINE_PATTERNS: dict[str, tuple[str, ...]] = {
    "false_invoice": (
        r"虚开", r"买票", r"买发票", r"代开票", r"找票", r"开票冲(?:成本|费用)",
        r"没有真实交易.{0,6}开票", r"多开(?:发票|票)",
    ),
    "conceal_income": (
        # "不想入账"这类说法中间会夹字（不想/不用/没法），所以留 4 个字的余量
        r"不.{0,4}(?:入账|记账|走账)", r"账外", r"隐瞒收入", r"少计收入",
        r"私户收(?:款|钱)", r"两套账", r"内账",
    ),
    "false_declaration": (
        r"虚假申报", r"做假账", r"伪造(?:凭证|资料|发票)", r"变造",
    ),
    "fraud_preference": (
        r"虚构.{0,8}(?:条件|资料|业务|人员)", r"包装成(?:高新|小微企业)",
        r"假(?:研发|培训|用工)", r"骗(?:取)?(?:税收)?优惠",
    ),
    "dual_contract": (
        r"阴阳合同", r"两套合同", r"[两2]\s*份合同", r"内外(?:两)?份合同", r"对(?:外|内)一份合同",
    ),
    "shell_business": (
        r"空壳", r"无(?:实际)?经营.{0,6}(?:注册|开票|享受)", r"注册即(?:可)?享受",
        r"只注册不经营", r"税收洼地.{0,8}(?:注册|开票)",
    ),
    "false_land": (
        r"虚假.{0,4}土地", r"虚构.{0,6}土地", r"虚增.{0,4}土地.{0,6}(?:出让金|成本)",
    ),
    "transfer_price": (
        r"关联.{0,8}(?:定价|交易).{0,12}(?:避税|少缴|调节利润|低税率)",
        r"转移利润", r"利润.{0,6}转(?:移|到)",
    ),
}

# 替代路径：每条红线对应"如果目的是 X，合规的做法是 Y"。
# 这是拒绝时最关键的一段话——只拒绝不给路，等于把用户推向更野的中介。
_ALTERNATIVES: dict[str, str] = {
    "false_invoice": "如果目的是增加成本抵减利润，合规做法是据实取得与真实业务一致的发票；成本不足应调整经营或如实申报，而不是用票凑数。",
    "conceal_income": "如果目的是降低税负，合规做法是用足小微企业、小规模纳税人等优惠，或通过合法的主体与业务安排降低税基。",
    "false_declaration": "如果目的是少缴税，合规做法是核对适用政策、检查是否有应享未享的优惠，并如实申报。",
    "fraud_preference": "如果目的是享受某项优惠但条件不满足，合规做法是评估能否通过真实调整（人员、研发投入、业务结构）达到条件；达不到就不能享受。",
    "dual_contract": "如果目的是控制税负，合规做法是在一份真实合同中合理约定交易条款（价格构成、时点、结算方式），而不是做两套合同。",
    "shell_business": "如果目的是享受区域性优惠，合规做法是在当地有真实经营（人员、场所、业务决策），否则不能享受。",
    "false_land": "如果目的是降低土地增值税，合规做法是据实核算扣除项目，并按现行规定判断是否适用相关优惠。",
    "transfer_price": "如果目的是集团整体税负优化，合规做法是按独立交易原则定价，并可考虑符合条件的特殊性税务处理。",
}


@dataclass
class RedLineHit:
    """一次红线命中。"""

    code: str
    name: str
    matched_text: str
    description: str = ""
    alternative: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "matched_text": self.matched_text,
            "description": self.description,
            "alternative": self.alternative,
        }

    def explain(self) -> str:
        lines = [f"该诉求涉及「{self.name}」类违法风险：{self.description}"]
        lines.append(f"识别到表述：“{self.matched_text}”")
        if self.alternative:
            lines.append(self.alternative)
        return "\n".join(lines)


class RedLineMatcher:
    """按红线清单匹配文本。"""

    def __init__(self, path: str | pathlib.Path | None = None) -> None:
        file_path = pathlib.Path(path) if path else _DEFAULT_RISK_RULES
        data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        self.red_lines = {item["code"]: item for item in data.get("red_lines") or []}
        self.output_control = data.get("output_control") or {}

    def match(self, text: str) -> list[RedLineHit]:
        hits: list[RedLineHit] = []
        if not text:
            return hits
        for code, patterns in _RED_LINE_PATTERNS.items():
            for pattern in patterns:
                found = re.search(pattern, text)
                if not found:
                    continue
                spec = self.red_lines.get(code, {})
                hits.append(
                    RedLineHit(
                        code=code,
                        name=spec.get("name", code),
                        matched_text=found.group(0),
                        description=spec.get("description", ""),
                        alternative=_ALTERNATIVES.get(code, ""),
                    )
                )
                break  # 同一条红线命中一次就够，不重复报
        return hits

    def risk_level_config(self, level: str) -> dict:
        """取某个风险等级的输出控制配置（green / yellow / red）。"""

        return (self.output_control.get("risk_levels") or {}).get(level) or {}

    @property
    def planning_enabled(self) -> bool:
        """筹划功能总开关。资质主体（F2）落实前保持关闭。"""

        return bool(self.output_control.get("planning_enabled"))

    def alternative_for(self, code: str) -> str:
        return _ALTERNATIVES.get(code, "")

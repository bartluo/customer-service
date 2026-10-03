"""官方"时效性"字段 → 本项目效力状态的翻译（ADR-0006 的落地）。

为什么必须翻译而不是直接存：国家税务总局政策法规库的"时效性"是它自己的口径
（全文有效 / 已修改 / 全文废止 / 全文失效 / 尚未生效），
本项目的效力状态是 ADR-0006 定稿的六种。两套枚举混用，
检索硬过滤（`CITABLE_EFFECT_STATUSES`）就会失准——
表现为"已废止的法规照样被引出来"。

映射依据 ADR-0006 的迁移说明：
  · `amended`（已修改）→ `superseded`：被修订后旧版本不再可直接引用，须改引新版
  · `expired`（失效）→ `repealed`：并入"已废止"；单纯超过有效期由时间区间表达
"""

from __future__ import annotations

# 官方时效性 → 本项目 effect_status
_OFFICIAL_AGING_MAP: dict[str, str] = {
    "全文有效": "effective",
    "部分失效": "partially_repealed",
    "已修改": "superseded",
    "全文废止": "repealed",
    "全文失效": "repealed",
    "尚未生效": "not_yet_effective",
}


def from_official_aging(aging: str | None) -> str | None:
    """把官方"时效性"翻译成本项目效力状态；认不出返回 None（交给调用方兜底）。

    认不出时返回 None 而不是默认成 effective：默认成有效等于把
    "不认识的都当能引用"，方向反了。
    """

    if not aging:
        return None
    return _OFFICIAL_AGING_MAP.get(aging.strip())


def is_citable(effect_status: str | None) -> bool:
    """这条法规能不能直接作为答案依据。"""

    from app.knowledge.ontology import CITABLE_EFFECT_STATUSES

    return (effect_status or "") in CITABLE_EFFECT_STATUSES

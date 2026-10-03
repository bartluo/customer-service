"""效力状态（时效性）的回归测试。

背景（2026-10-01 D 专题）：
  官方政策法规库对每份文件都标了时效性，但采集时统一按"现行有效"入库，
  结果是 **45 份已废止、29 份已被替代的法规照样被检索引出来**——
  问增值税时答案里可能引用《增值税暂行条例》（2026 年已废止）。
  这不是数据多少的问题，是答案对错的问题。
"""

from __future__ import annotations

import pytest

from app.domain.effect_status import from_official_aging, is_citable
from app.knowledge.ontology import CITABLE_EFFECT_STATUSES


@pytest.mark.parametrize(
    ("aging", "expected"),
    [
        ("全文有效", "effective"),
        ("部分失效", "partially_repealed"),
        # ADR-0006：被修订后旧版本不再可直接引用，须改引新版
        ("已修改", "superseded"),
        ("全文废止", "repealed"),
        ("全文失效", "repealed"),
        ("尚未生效", "not_yet_effective"),
    ],
)
def test_official_aging_maps_to_effect_status(aging: str, expected: str) -> None:
    assert from_official_aging(aging) == expected


@pytest.mark.parametrize("aging", ["", None, "未知口径", "   "])
def test_unknown_aging_returns_none_not_effective(aging) -> None:
    """认不出必须返回 None，不能默认成"现行有效"。

    默认成有效等于"不认识的都当能引用"，方向反了：
    宁可标不出来交给人工，也不能把来源不明的文件当依据。
    """

    assert from_official_aging(aging) is None


def test_only_effective_and_partially_repealed_are_citable() -> None:
    """可引用白名单只有两种：现行有效、部分废止。"""

    assert is_citable("effective")
    assert is_citable("partially_repealed")
    for status in ("repealed", "superseded", "draft", "not_yet_effective", None):
        assert not is_citable(status)


def test_citable_list_matches_ontology() -> None:
    """白名单本身要与 ontology 定义一致，防止两处漂移。"""

    assert set(CITABLE_EFFECT_STATUSES) == {"effective", "partially_repealed"}

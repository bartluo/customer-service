"""税种识别与税种过滤的回归测试。

背景（2026-09-30 修的一串问题）：
  1. 采集侧把 82 份新法规的税种全标成了"增值税"，《个人所得税法》的标签也是"增值税"；
  2. 检索侧只认术语表里登记的几十个口语别名，用户直接写税种全名时一个都认不出，
     等于没加税种过滤，检索结果飘到别的税种；
  3. 术语表里的"所得税"会匹配进"个人所得税"，用户问个税、系统去搜企业所得税。

这三条都会让"按税种过滤"这条链路悄悄出错，所以用测试锁住。
"""

from __future__ import annotations

from app.domain.tax_types import TAX_TYPES, detect_tax_types
from app.retrieval.glossary import GlossaryExpander
from app.services.ingest.collector import _guess_tax_types, tax_types_from_tax_policy


# ---------- 文本里的税种识别 ----------


def test_detects_simple_tax_type() -> None:
    assert detect_tax_types("个人所得税怎么算") == ["个人所得税"]
    assert detect_tax_types("房产税如何计算") == ["房产税"]


def test_longer_name_wins_over_substring() -> None:
    """"土地增值税"不能被"增值税"抢走。"""

    assert detect_tax_types("土地增值税怎么算") == ["土地增值税"]
    assert detect_tax_types("城镇土地使用税怎么交") == ["城镇土地使用税"]


def test_detects_multiple_tax_types() -> None:
    """同一句话涉及多个税种时都要识别出来。"""

    found = detect_tax_types("小微企业增值税和企业所得税有什么优惠")
    assert "增值税" in found
    assert "企业所得税" in found


def test_no_tax_type_returns_empty() -> None:
    assert detect_tax_types("发票丢了怎么办") == ["发票"]
    assert detect_tax_types("公司注销要走什么流程") == []


# ---------- 术语扩展（检索加过滤时用） ----------


def test_expand_tax_types_prefers_full_name_over_alias() -> None:
    """"个人所得税"不能因为含"所得税"而被判成企业所得税。"""

    expander = GlossaryExpander()
    assert expander.expand_tax_types("个人所得税怎么算") == ["个人所得税"]
    assert expander.expand_tax_types("个人所得税专项附加扣除") == ["个人所得税"]


def test_expand_tax_types_still_handles_colloquial_aliases() -> None:
    """口语说法仍然要认得出来（术语表的老职责不能丢）。"""

    expander = GlossaryExpander()
    assert expander.expand_tax_types("小规模怎么交税") == ["增值税"]
    assert expander.expand_tax_types("个税怎么算") == ["个人所得税"]
    assert expander.expand_tax_types("进项能不能抵扣") == ["增值税"]


def test_expand_tax_types_empty_for_generic_question() -> None:
    """泛化问题不加税种过滤，避免把不该排除的法规排除掉。"""

    expander = GlossaryExpander()
    assert expander.expand_tax_types("公司注销要走什么流程") == []


def test_expand_tax_types_covers_social_insurance_and_disability_fund() -> None:
    """社保费与残保金不是税，但客户就是按这两个简称来问的。"""

    expander = GlossaryExpander()
    assert expander.expand_tax_types("社保怎么缴") == ["社会保险费"]
    assert expander.expand_tax_types("社会保险费的缴费基数怎么定") == ["社会保险费"]
    assert expander.expand_tax_types("残保金怎么算") == ["残疾人就业保障金"]
    assert expander.expand_tax_types("残疾人就业保障金优惠政策") == ["残疾人就业保障金"]


# ---------- 采集侧的税种标注 ----------


def test_tax_policy_translates_to_tax_type() -> None:
    """官方"税费类型"字段是权威来源，优先采信。"""

    assert tax_types_from_tax_policy("税收政策-个人所得税") == ["个人所得税"]
    assert tax_types_from_tax_policy("税收政策-土地增值税") == ["土地增值税"]
    assert tax_types_from_tax_policy("税收政策-环境保护税") == ["环境保护税"]


def test_tax_policy_unknown_falls_back_to_empty() -> None:
    """认不出的官方分类不硬猜，交给后面的兜底逻辑。"""

    assert tax_types_from_tax_policy("税收政策-其他税收政策") == []
    assert tax_types_from_tax_policy("其他") == []
    assert tax_types_from_tax_policy(None) == []


def test_guess_prefers_official_tax_policy_over_collection_code() -> None:
    """关键回归：栏目分类码 cn_tax_admin 不能一律被当成增值税。

    这是当初 82 份法规全被标成"增值税"的直接原因。
    """

    assert _guess_tax_types("cn_tax_admin", "本法所称各项个人所得……", tax_policy="税收政策-个人所得税") == [
        "个人所得税"
    ]


def test_guess_merges_official_and_title_tax_types() -> None:
    """官方分类与标题识别是不同维度，要取并集。

    增值税专用发票类公告官方分类是"增值税"，但用户是照着"发票"来问的。
    只留官方维度会让"发票丢了怎么办"这类提问把这类公告过滤掉。
    """

    guessed = _guess_tax_types(
        None,
        "现将增值税专用发票有关事项公告如下……",
        title="国家税务总局关于被盗、丢失增值税专用发票有关问题的公告",
        tax_policy="税收政策-增值税",
    )
    assert "增值税" in guessed
    assert "发票" in guessed


def test_guess_falls_back_to_title_then_body() -> None:
    """没有官方分类时：先看标题，标题无线索才扫正文。"""

    assert _guess_tax_types(None, "契税的纳税人为……", title="中华人民共和国契税法") == ["契税"]

    body = "增值税……" * 5 + "消费税……" * 2
    guessed = _guess_tax_types(None, body, title="关于某事项的公告")
    assert guessed[0] == "增值税"
    assert len(guessed) <= 2  # 正文兜底最多取两个，避免打一堆标签


def test_known_tax_types_are_unique() -> None:
    assert len(TAX_TYPES) == len(set(TAX_TYPES))

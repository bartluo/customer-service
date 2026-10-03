"""执行期限识别的回归测试（D 专题）。

背景：官方政策法规库对财政部/税务总局联合发文**不标时效性**，
这类文件的执行期限只能从正文里读：
    "本公告执行期限为2023年1月1日至2024年12月31日"
    "本公告执行至2027年12月31日"
识别错了的后果和"标记错效力状态"一样——要么把现行政策当过期，
要么把过期政策当现行。
"""

from __future__ import annotations

from app.domain.validity import find_window, parse_date


def test_parses_chinese_date() -> None:
    assert parse_date("2024年12月31日") == "2024-12-31"
    assert parse_date("2024年1月5日") == "2024-01-05"
    assert parse_date("没有日期") is None
    assert parse_date(None) is None


def test_window_with_start_and_end() -> None:
    text = "本公告执行期限为2023年1月1日至2024年12月31日。"
    assert find_window(text) == ("2023-01-01", "2024-12-31")


def test_window_with_end_only() -> None:
    """只看得到结束日的写法（"执行至X""延长至X"）。"""

    assert find_window("五、本公告执行至2027年12月31日。") == (None, "2027-12-31")
    assert find_window("执行期限延长至2022年3月31日。") == (None, "2022-03-31")
    assert find_window("本通知有效期至2025年12月31日。") == (None, "2025-12-31")


def test_takes_the_latest_end_date() -> None:
    """一份文件多处提到期限时，以结束日最晚的那一处为准。

    政策常被"延长"，文中会同时出现原期限和延长后的期限，
    取错就会把仍在执行的政策判成过期。
    """

    text = (
        "本公告执行期限为2021年1月1日至2021年12月31日。"
        "根据后续规定，执行期限延长至2027年12月31日。"
    )
    start, end = find_window(text)
    assert end == "2027-12-31"


def test_no_window_returns_nothing() -> None:
    """法律、行政法规通常不写执行期限，不能硬凑一个出来。"""

    assert find_window("第一条 在中华人民共和国境内销售货物的单位和个人为纳税人。") == (
        None,
        None,
    )
    assert find_window("") == (None, None)


def test_does_not_match_unrelated_date_pairs() -> None:
    """无关的日期对（如成文日期、施行日期）不能被当成执行期限。"""

    text = "本公告自2023年1月1日起施行。财政部 税务总局 2023年3月26日"
    # "自X日起施行" 不是执行区间，不能识别成窗口
    assert find_window(text) == (None, None)

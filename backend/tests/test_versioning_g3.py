"""条文版本与效力状态推断测试（版本区间 + 效力状态）。

对应技术方案 4.1：
  · 每个条文版本带 [生效日, 失效日)，区间不重叠
  · 六种效力状态正确标记（ADR-0006）
  · 废止可精确到条/款，整部有效但某条失效必须支持

这些是纯逻辑，不连数据库，因此可以快速反复跑。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.knowledge.versioning import (
    AmendmentResult,
    apply_amendment,
    apply_repeal,
    compute_effect_status,
    detect_repeal_targets,
    status_as_of,
    utc,
)


def test_compute_effect_status_marks_future_not_yet_effective() -> None:
    """尚未到施行日：not_yet_effective，不得作为现行依据。"""

    status = compute_effect_status(
        valid_from=utc(2030, 1, 1),
        valid_to=None,
        as_of=date(2026, 9, 29),
    )
    assert status == "not_yet_effective"


def test_compute_effect_status_marks_current_effective() -> None:
    """在有效期内：effective。"""

    status = compute_effect_status(
        valid_from=utc(2020, 1, 1),
        valid_to=None,
        as_of=date(2026, 9, 29),
    )
    assert status == "effective"


def test_compute_effect_status_after_expiry() -> None:
    """超过失效日：不是 effective。"""

    status = compute_effect_status(
        valid_from=utc(2020, 1, 1),
        valid_to=utc(2025, 1, 1),
        as_of=date(2026, 9, 29),
    )
    assert status != "effective"


def test_status_as_of_boundary_is_left_closed_right_open() -> None:
    """左闭右开：valid_from 当天有效，valid_to 当天已失效。"""

    valid_from = utc(2020, 1, 1)
    valid_to = utc(2025, 1, 1)

    assert status_as_of(valid_from, valid_to, date(2024, 12, 31)) == "effective"
    # valid_to 当天即失效
    assert status_as_of(valid_from, valid_to, date(2025, 1, 1)) != "effective"


def test_compute_effect_status_respects_repealed_flag() -> None:
    """已废止优先于区间判断：即使区间还在，也不可引用。"""

    status = compute_effect_status(
        valid_from=utc(2020, 1, 1),
        valid_to=None,
        as_of=date(2026, 9, 29),
        declared_repealed=True,
    )
    assert status == "repealed"


def test_detect_repeal_targets_from_text() -> None:
    """从正文识别"同时废止"的清单。"""

    text = (
        "第二条 财政部 税务总局关于深化增值税改革有关问题的公告〔2019〕38号同时废止。\n"
        "第三条 国税发〔1994〕155号文件自本公告发布之日起废止。"
    )
    targets = detect_repeal_targets(text)

    numbers = [t["document_number"] for t in targets]
    assert any("2019" in (n or "") for n in numbers)
    assert len(targets) >= 2
    for target in targets:
        assert target["evidence"]


def test_detect_repeal_targets_returns_empty_when_none() -> None:
    """没有废止表述时返回空列表，不误报。"""

    assert detect_repeal_targets("第一条 本公告自2020年1月1日起施行。") == []


def test_apply_amendment_closes_previous_interval() -> None:
    """修改后：旧版本失效日 = 新版本生效日，区间首尾相接不重叠。"""

    previous = AmendmentResult(
        version=1,
        valid_from=utc(2019, 3, 20),
        valid_to=utc(2022, 1, 1),
        content="原条文内容",
        effect_status="effective",
    )
    amended = apply_amendment(
        previous,
        new_content="修改后的条文内容",
        amend_date=utc(2022, 1, 1),
        basis="财税〔2022〕1号",
    )

    assert amended.version == 2
    assert amended.valid_from == utc(2022, 1, 1)
    assert amended.valid_to is None
    assert amended.content == "修改后的条文内容"
    # 关键：旧版本失效日必须被补上，与新版生效日接上
    assert previous.valid_to == utc(2022, 1, 1)


def test_apply_amendment_is_idempotent_on_same_date() -> None:
    """同一天重复应用修改不应产生零长度区间。"""

    previous = AmendmentResult(
        version=1,
        valid_from=utc(2022, 1, 1),
        valid_to=None,
        content="内容",
        effect_status="effective",
    )
    apply_amendment(previous, new_content="新内容", amend_date=utc(2022, 1, 1), basis="文号")
    amended = apply_amendment(previous, new_content="更新的内容", amend_date=utc(2022, 1, 1), basis="文号")

    assert amended.valid_from == utc(2022, 1, 1)
    assert previous.valid_to is None  # 已生效版本不被同日修改截断


def test_apply_repeal_marks_status_and_closes_interval() -> None:
    """废止：状态变 repealed，失效日 = 废止日。"""

    previous = AmendmentResult(
        version=1,
        valid_from=utc(2019, 3, 20),
        valid_to=None,
        content="原内容",
        effect_status="effective",
    )
    repealed = apply_repeal(previous, repeal_date=utc(2023, 6, 1), basis="财税〔2023〕10号")

    assert repealed.effect_status == "repealed"
    assert repealed.valid_to == utc(2023, 6, 1)
    assert previous.valid_to == utc(2023, 6, 1)

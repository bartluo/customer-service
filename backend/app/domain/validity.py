"""从法规正文里识别执行期限（D 专题：时效性治理）。

为什么需要它：
  官方政策法规库**只对一部分文件标时效性**。财政部/税务总局联合发文的
  税收优惠公告，页面上根本没有时效性字段——这类文件靠官方数据永远补不全。

  但它们有个共同点：正文里明写着执行期限。
      "本公告执行期限为2023年1月1日至2024年12月31日"
      "本公告执行至2027年12月31日"
      "执行期限延长至2022年3月31日"
  这句话就是权威依据，比人工猜可靠。

只做识别，不碰数据库——识别是纯函数，便于单测；写库由脚本负责。
"""

from __future__ import annotations

import re

_DATE = r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
_DATE_PARSE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# 带起止日的写法：能同时拿到 valid_from 和 valid_to
WINDOW_PATTERNS = (
    # 执行期限为 A 至 B / 执行期限自 A 起至 B / 自 A 起至 B
    re.compile(
        rf"(?:执行期限[为自]?|自)\s*(?P<start>{_DATE})[^。；\n]{{0,12}}?至\s*(?P<end>{_DATE})"
    ),
    # A 日至 B 日（同一句里给出两个日期）
    re.compile(rf"(?P<start>{_DATE})\s*(?:起)?至\s*(?P<end>{_DATE})"),
)

# 只有结束日的写法
END_ONLY_PATTERNS = (
    re.compile(rf"执行期限[^。；\n]{{0,30}}?至\s*(?P<end>{_DATE})"),
    re.compile(rf"执行至\s*(?P<end>{_DATE})"),
    re.compile(rf"(?:延长|延期|延续)[^。；\n]{{0,20}}?至\s*(?P<end>{_DATE})"),
    re.compile(rf"有效期(?:限)?(?:至|到)\s*(?P<end>{_DATE})"),
)


def parse_date(text: str | None) -> str | None:
    """把"2024年12月31日"转成 2024-12-31。"""

    match = _DATE_PARSE.search(text or "")
    if not match:
        return None
    year, month, day = (int(match.group(i)) for i in (1, 2, 3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def find_window(text: str) -> tuple[str | None, str | None]:
    """找文档里的执行区间，返回 (起始日, 结束日)。

    一份文件可能多处提到期限（如"延长至X"），取**结束日最晚**的那一处，
    因为那才是当前有效口径。
    """

    best: tuple[str | None, str | None] = (None, None)
    for pattern in WINDOW_PATTERNS:
        for match in pattern.finditer(text or ""):
            start = parse_date(match.group("start"))
            end = parse_date(match.group("end"))
            if end and (best[1] is None or end > best[1]):
                best = (start, end)

    for pattern in END_ONLY_PATTERNS:
        for match in pattern.finditer(text or ""):
            end = parse_date(match.group("end"))
            if end and (best[1] is None or end > best[1]):
                best = (None, end)
    return best

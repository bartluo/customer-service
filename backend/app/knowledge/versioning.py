"""条文版本与效力状态推断。

这是财税系统最核心的模块。两条业务事实决定了它的形态：
  1. 法规会被后续文件"改条款"。同一部法规的第七条可能在 2022 年被改过三次，
     每次修改都产生一个新版本，内容不同、生效区间不同。
  2. 废止是常态。税务规范性文件的有效期通常只有 1～3 年，到期就整体失效。

两个容易写错、这里明确处理的点：
  · 区间语义是左闭右开 [valid_from, valid_to)。修改一条时，旧版本的失效日
    必须补成新版本的生效日，否则会在旧区间尾部留一个"空洞"或重叠。
  · 效力状态不等于区间是否覆盖今天。已明文废止的条文，即使区间上还"有效"，
    也绝不能被引用（ontology.yaml no_cite_repealed，blocking 级）。
"""

from __future__ import annotations
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from app.knowledge.ontology import EFFECT_STATUSES, is_citable


def utc(year: int, month: int, day: int) -> datetime:
    """构造带时区的 datetime。法规日期是"日"粒度，统一按 UTC 正午处理，
    避免时区偏移把某一天挪到前一天或后一天。
    """

    return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)


def _to_date(value: datetime | date | str | None) -> date | None:
    """把多种日期写法统一成 date，便于区间比较。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def status_as_of(
    valid_from: datetime | date | str,
    valid_to: datetime | date | str | None,
    as_of: datetime | date | str,
) -> str:
    """判断某个时点在不在这个版本的生效区间内（纯几何判断，不看效力状态）。

    区间语义 [valid_from, valid_to)：valid_to 当天已失效。
    返回 "effective" 或 "out_of_range"。
    """

    start = _to_date(valid_from)
    end = _to_date(valid_to)
    query = _to_date(as_of)
    if start is None or query is None:
        return "out_of_range"
    if query < start:
        return "out_of_range"
    if end is not None and query >= end:
        return "out_of_range"
    return "effective"


def compute_effect_status(
    valid_from: datetime | date | str,
    valid_to: datetime | date | str | None,
    as_of: datetime | date | str,
    declared_repealed: bool = False,
    is_draft: bool = False,
) -> str:
    """综合区间与显式声明，算出当前效力状态（ADR-0006 六种之一）。

    判断顺序有讲究：
      1. 草案最优先。征求意见稿无论日期如何都不可引用。
      2. 已声明废止的优先于区间。区间可能还没到期但文件已被宣布废止。
      3. 其余按区间几何判断：未来 → not_yet_effective，在期内 → effective，
         已过期 → repealed（到期失效在业务上等同于不再有效）。

    "已过期"归入 repealed 而不是单列 expired 状态，是 ADR-0006 的决定：
    过期与否由区间表达，不需要额外状态。
    """

    if is_draft:
        return "draft"
    if declared_repealed:
        return "repealed"

    start = _to_date(valid_from)
    query = _to_date(as_of)
    if start is None or query is None:
        return "not_yet_effective"
    if query < start:
        return "not_yet_effective"
    if status_as_of(valid_from, valid_to, as_of) == "effective":
        return "effective"
    return "repealed"


@dataclass
class AmendmentResult:
    """一个条文版本的快照。

    用 dataclass 而不是 ORM 对象，是为了让这段逻辑保持可单测，
    不需要数据库也能验证版本推演是否正确。
    """

    version: int
    valid_from: datetime
    valid_to: datetime | None
    content: str
    effect_status: str
    basis: str | None = None

    def __post_init__(self) -> None:
        if self.effect_status not in EFFECT_STATUSES:
            raise ValueError(f"非法效力状态：{self.effect_status}")


def _close_previous(previous: AmendmentResult, boundary: datetime) -> None:
    """给旧版本补上失效日，保证与新区间首尾相接。

    同日修改的特殊情况：如果旧版本的生效日已经不早于 boundary，
    说明它还没对外生效过就被改掉了。此时不应把旧版本截断成零长度区间
    （valid_to == valid_from 会被数据库 ck_version_valid_range 拒绝），
    因此保持旧区间不动，让新版本整体取代它。
    """

    if previous.valid_from >= boundary:
        return
    previous.valid_to = boundary


def apply_amendment(
    previous: AmendmentResult,
    new_content: str,
    amend_date: datetime,
    basis: str | None = None,
) -> AmendmentResult:
    """对某条文应用一次修改，返回新版本。

    会同时更新 previous 的失效日 —— 这是最容易漏掉的一步：
    漏了会导致数据库排除约束报错，或旧版本永远停留在"有效"状态。
    """

    _close_previous(previous, amend_date)
    return AmendmentResult(
        version=previous.version + 1,
        valid_from=amend_date,
        valid_to=None,
        content=new_content,
        effect_status="effective",
        basis=basis,
    )


def apply_repeal(
    previous: AmendmentResult,
    repeal_date: datetime,
    basis: str | None = None,
) -> AmendmentResult:
    """对某条文应用废止，返回状态为 repealed 的版本。

    返回的是"同一区间、状态改为 repealed"的版本，
    而不是在末尾追加一个零长度版本 —— 废止不改变内容，只改变效力。
    """

    _close_previous(previous, repeal_date)
    return replace(
        previous,
        valid_to=repeal_date,
        effect_status="repealed",
        basis=basis,
    )


# ---------- 废止目标识别 ----------

# 只抓明确带"废止"动词的句子，避免把普通的"依照""根据"误判为废止关系。
_REPEAL_KEYWORD = "废止"
# 三种文号写法都要认，缺一都会导致废止关系漏抽：
#   1. 短文号：国税发〔1994〕155号
#   2. 公告式：财政部 税务总局公告2019年第39号 / 税务总局公告2011年第13号
#   3. 长文号：财政部 税务总局关于深化增值税改革有关问题的公告〔2019〕38号
#      （第 3 种是公告最常见的写法，缺了它废止关系会大量漏抽）
_DOC_NUMBER_TOKEN = re.compile(
    r"(?P<number>[一-龥][一-龥\s]{0,40}?(?:公告|通告|通知|文件|令)"
    r"(?:〔|\[|［)?\s*\d{2,4}\s*(?:〕|\]|］)?\s*第?\s*\d+\s*号"
    r"|[一-龥][一-龥\s]{0,40}?(?:公告|通告|通知|文件|令)\s*\d{4}\s*年\s*第\s*\d+\s*号"
    r"|[一-龥]{2,12}(?:〔|\[|［)\s*\d{2,4}\s*(?:〕|\]|］)\s*第?\s*\d+\s*号)"
)


def detect_repeal_targets(text: str) -> list[dict[str, str]]:
    """从法规原文中找出"被废止的其它文件"。

    用于建立 repealed_by / repeals 关系边，让系统能回答
    "这条旧规定什么时候被谁废了"。

    只在同一句里同时出现"文号"和"废止"时才判定，避免跨句误配。
    返回 [{document_number, evidence}]，找不到就返回空列表。
    """

    if not text:
        return []

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for sentence in re.split(r"[。；;\n]", text):
        sentence = sentence.strip()
        if not sentence or _REPEAL_KEYWORD not in sentence:
            continue
        for match in _DOC_NUMBER_TOKEN.finditer(sentence):
            number = re.sub(r"\s+", "", match.group("number"))
            number = (
                number.replace("[", "〔")
                .replace("［", "〔")
                .replace("]", "〕")
                .replace("］", "〕")
            )
            if number in seen:
                continue
            seen.add(number)
            results.append({"document_number": number, "evidence": sentence})

    return results


def is_citable_version(version: AmendmentResult, as_of: datetime | date | str) -> bool:
    """能否在 as_of 时点把这一版作为答案依据。

    检索硬过滤与答案生成共用这一个判断，保证两处口径一致。
    """

    if not is_citable(version.effect_status):
        return False
    return status_as_of(version.valid_from, version.valid_to, as_of) == "effective"

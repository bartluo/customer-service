"""法规解析器：把原始法规文本拆成结构化条文。

输入：一份法规的纯文本（来自采集或上传）。
输出：ParsedRegulation（元数据 + 条文列表），可直接写入 regulations /
  regulation_articles / regulation_article_versions 三张表。

三条设计原则（全部来自技术方案与 ontology.yaml）：
  1. 抽不到就标待复核，绝不编造。文号、发文机关、施行日期任一识别不出，
     就把 requires_review 置 True 并写明原因，由审核专家处理。
  2. 层级切分以正则锚定行首为准，避免把正文里的"第一条"误当成条文标题。
  3. 附则、过渡条款必须保留：它们常规定"自X年X月X日起施行"和废止清单，
     丢了会导致整部法规的时效信息缺失。
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.knowledge.ontology import HIERARCHY_LEVELS

# ---------- 中文数字 ----------
_CN_DIGITS = "一二三四五六七八九十百零〇两"

# 层级正则：锚定行首，顺序即优先级（章 > 节 > 条 > 款 > 项）
_CHAPTER_RE = re.compile(rf"^第[{_CN_DIGITS}\d]+章[\s　]*(.*)$")
_SECTION_RE = re.compile(rf"^第[{_CN_DIGITS}\d]+节[\s　]*(.*)$")
_ARTICLE_RE = re.compile(rf"^(第[{_CN_DIGITS}\d]+条)[\s　]*(.*)$")
_PARAGRAPH_RE = re.compile(rf"^[（(]([{_CN_DIGITS}\d]+)[)）][\s　]*(.*)$")
_ITEM_RE = re.compile(r"^(\d+[、.．])[\s　]*(.*)$")

# 总局公告 / 通知常用"一、二、三、"分条编号，没有"第X条"。
# 这类文件在财税里极常见（小规模纳税人优惠、研发费用加计扣除、印花税口径等），
# 不识别就会整份法规切不出任何条文，属于严重漏采。
# 与"项"的区别：一、二、 是中文数字；项是阿拉伯数字 1. 2.
_CN_ITEM_RE = re.compile(rf"^([{_CN_DIGITS}]+[、．])[\s　]*(.*)$")

# 文号：两种写法都要认。
#   旧式：财税〔2019〕39号、国税发〔1994〕155号、财税[2019]39号
#   新式：财政部 税务总局 海关总署公告2019年第39号、国家税务总局公告2011年第13号
_DOC_NUMBER_BRACKET_RE = re.compile(
    r"^[\u4e00-\u9fa5]{2,12}[〔\[［\[](\d{4})[〕\]］\]]\s*第?\s*(\d+)\s*号$"
)
_DOC_NUMBER_NEW_RE = re.compile(
    r"^(?P<issuer>[\u4e00-\u9fa5、，\s]{2,40}?)(?:公告|通告|令|函)"
    r"(?P<year>\d{4})\s*年\s*第\s*(?P<no>\d+)\s*号$"
)
_DOC_NUMBER_ORDER_RE = re.compile(rf"^第\s*\d+\s*号\s*令$")

# 主席令 / 国务院令 / 部委令 这类"文号行"，官方库里有，但正则识别不出具体文号，
# 只用来判断"首行是文号行还是标题行"。
_DOC_NUMBER_LIKE_RE = re.compile(
    r"(?:^(?:中华人民共和国)?(?:主席令|国务院令|财政部令|国家税务总局令))"
    r"|(?:^第\s*[\d一二三四五六七八九十百]+\s*号\s*令$)"
)

# 施行日期：自X年X月X日起施行 / 起执行 / 起施行
_EFFECTIVE_RE = re.compile(
    r"自\s*(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日\s*起\s*(?:施行|执行|实施)"
)
_REPEAL_DATE_RE = re.compile(
    r"自\s*(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日\s*起\s*(?:废止|失效|停止执行)"
)
_PUBLISH_DATE_RE = re.compile(
    r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
)

# 位阶判定：按技术方案 4.1 的顺序，从法律往低匹配
_HIERARCHY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("law", re.compile(r"^中华人民共和国.*法$")),
    ("administrative_regulation", re.compile(r".*条例$")),
    ("departmental_rule", re.compile(r".*办法$")),
    ("normative_document", re.compile(r".*(?:公告|通告|批复|通知)$")),
    ("local_normative", re.compile(r".*(?:省|市|自治区).*(?:税务局|人民政府)?(?:公告|通知)$")),
)

_ISSUER_PATTERN = re.compile(
    r"(财政部|国家税务总局|税务总局|海关总署|国务院|财政部和国家税务总局|"
    r"国家税务总局和财政部|财政部、税务总局)"
)

# 整篇即一条时的兜底阈值与编号。
# 太短的多半是页脚残留或"现予公布《XX》"这类没有实体内容的通知，不进知识库。
MIN_WHOLE_TEXT_CHARS = 30
WHOLE_TEXT_FULL_NO = "全文"


def _whole_text_body(lines: list[str], document_number: str | None, title: str) -> str:
    """取"正文部分"：去掉首部的文号行与标题行。

    文件约定是首行文号、次行标题（没有文号时首行即标题），
    这里把这几行去掉，剩下的才算内容。
    """

    skip = {line.strip() for line in (document_number or "", title or "") if line.strip()}
    body_lines: list[str] = []
    for index, raw in enumerate(lines):
        line = raw.strip()
        if index < 3 and (not line or line in skip):
            continue
        body_lines.append(line)
    return "\n".join(part for part in body_lines if part).strip()


@dataclass
class ParsedBlock:
    """切出的一段结构（章、节、条、款、项）。"""

    level_code: str
    article_no: str
    content: str
    full_no: str
    heading_path: str
    order_index: int = 0
    parent_index: int | None = None


@dataclass
class ParsedRegulation:
    """一份解析完成的法规。"""

    title: str
    document_number: str | None
    issuer: str | None
    hierarchy_level: str
    publish_date: datetime | None
    effective_date: datetime | None
    expiry_date: datetime | None
    blocks: list[ParsedBlock] = field(default_factory=list)
    source_url: str | None = None
    requires_review: bool = False
    review_reasons: list[str] = field(default_factory=list)

    @property
    def articles(self) -> list[ParsedBlock]:
        """只返回"条"级（引用单位的第一层）。"""

        return [b for b in self.blocks if b.level_code == "article"]


def _normalize_brackets(text: str) -> str:
    """把 [［  这三种左括号与 ]］  这三种右括号统一成〔〕。"""

    return (
        text.replace("[", "〔")
        .replace("［", "〔")
        .replace("]", "〕")
        .replace("］", "〕")
    )


def _match_document_number(text: str) -> tuple[str | None, str | None]:
    """识别文号与发文机关。返回 (文号, 发文机关)；识别不出返回 (None, None)。"""

    stripped = text.strip()
    if not stripped:
        return None, None

    lines = stripped.splitlines()
    candidate = lines[0].strip()
    candidate = _normalize_brackets(candidate)

    # 有些页面把文号拆成两行（"国家税务总局公告" / "2018年第55号"）。
    # 只取首行会把"国家税务总局公告"当成标题，文号也识别不出来。
    # 首行单独认不出时，把首行与第二行拼起来再试一次。
    joined = None
    if len(lines) > 1:
        joined = _normalize_brackets((lines[0].strip() + lines[1].strip())[:80])

    for value in (candidate, joined):
        if not value:
            continue
        result = _match_document_number_line(value)
        if result[0]:
            return result
    return None, None


def _match_document_number_line(candidate: str) -> tuple[str | None, str | None]:
    """按单行文本识别文号（旧式 / 新式 / 国务院令）。"""

    # 新式：财政部 税务总局 海关总署公告2019年第39号
    match = _DOC_NUMBER_NEW_RE.match(candidate)
    if match:
        issuer = re.sub(r"\s+", " ", match.group("issuer")).strip()
        return candidate, issuer

    # 旧式：财税〔2019〕39号
    match = _DOC_NUMBER_BRACKET_RE.match(candidate)
    if match:
        return candidate, _guess_issuer_from_prefix(candidate)

    # 国务院令
    if _DOC_NUMBER_ORDER_RE.match(candidate):
        return candidate, "国务院"

    return None, None


def _guess_issuer_from_prefix(document_number: str) -> str | None:
    """从旧式文号前缀推断发文机关。这是推断，不是编造文号本身。"""

    head = document_number[:3]
    mapping = {
        "财税": "财政部",
        "国税": "国家税务总局",
        "税总": "国家税务总局",
        "国发": "国务院",
    }
    return mapping.get(head)


def _match_date(text: str, pattern: re.Pattern[str]) -> datetime | None:
    match = pattern.search(text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _detect_hierarchy(title: str, issuer: str | None) -> str | None:
    for level, pattern in _HIERARCHY_RULES:
        if pattern.match(title):
            return level
    if issuer and ("省税务局" in issuer or "市税务局" in issuer):
        return "local_normative"
    return None


def split_articles(text: str) -> list[ParsedBlock]:
    """按 章 / 节 / 条 / 款 / 项 切分法规正文。

    规则：逐行扫描，遇到层级标记就开始一个新块，把后续行归入该块正文。
    这样"条"里的多个自然段（款）也能被正确识别，且不会误切。
    """

    if not text or not text.strip():
        return []

    blocks: list[ParsedBlock] = []
    chapter_head = ""
    section_head = ""
    current_kind: str | None = None
    current_heading = ""
    # 上下文类型：记录"当前所处的结构上下文"，不随 flush() 重置。
    # 为什么单独一个变量：flush() 之后 current_kind 会置 None（块已封存），
    # 但下一个"（二）"出现时我们仍需知道它属于哪个条，才能建对父子关系。
    # 曾因复用 current_kind 导致"（二）"被当成正文吞掉、项编号重复。
    context_kind: str | None = None
    # 每种层级最近一次出现的块下标，用于建立 parent_index
    last_index: dict[str, int] = {}
    # 完整编号出现次数，用于兜底去重（同一文档里同一条号出现两次的极端情况）
    seen_full_no: dict[str, int] = {}
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, current_kind, current_heading, last_index, context_kind, seen_full_no
        if current_kind is None:
            buffer = []
            return
        content = "\n".join(buffer).strip()

        if current_kind in {"paragraph", "item"}:
            # 项的父级是款，款的父级是条。两级都要建关系才能追溯完整编号。
            # 不带这个层级，"(一) 1." 与 "(二) 1." 会算出同一个 full_no，撞唯一约束。
            if current_kind == "paragraph":
                # 款直接挂在条下，不挂在上一个款下——否则"（二）"的编号会
                # 变成"第一条 （一） （二）"，与正文实际结构不符。
                parent_index = last_index.get("article")
            else:
                if _CN_ITEM_RE.match(current_heading):
                    # "一、二、三、" 是总局公告的顶层分条，直接挂在章/节下，
                    # 不挂在款或条下——这类公告本身没有"第X条"。
                    parent_index = last_index.get("section") or last_index.get("chapter")
                else:
                    parent_index = last_index.get("paragraph") or last_index.get("article")
        elif current_kind == "article":
            parent_index = last_index.get("section") or last_index.get("chapter")
        else:
            parent_index = None

        full_no = _build_full_no(chapter_head, section_head, current_heading, current_kind)
        if current_kind in {"paragraph", "item"} and parent_index is not None:
            # 款、项编号在各自父级下都会重复（每条都有"（一）"，每款都有"1."）。
            # 完整编号必须带整条父级链：从条一路到直接父级，逐层拼接。
            chain: list[str] = []
            cursor: int | None = parent_index
            while cursor is not None and len(chain) < 4:
                parent_block = blocks[cursor]
                if parent_block.level_code in {"article", "paragraph"}:
                    chain.append(parent_block.article_no)
                cursor = parent_block.parent_index
            base = _build_full_no(chapter_head, section_head, "", "paragraph_base")
            parts = [part for part in (base, " ".join(reversed(chain)), current_heading) if part]
            full_no = " ".join(parts)

        index = len(blocks)
        # 兜底去重：极少数文档里同一条号会出现两次（如把修订前后拼在一起），
        # 这时款号也会重复，且父级链完全相同，无法靠结构区分。
        # 加一个出现次数后缀，保证 full_no 唯一，导入不失败。
        # 这种情况应视为文档质量问题，由审核专家确认，不在解析层猜测内容。
        occurrence = seen_full_no.get(full_no, 0)
        seen_full_no[full_no] = occurrence + 1
        unique_full_no = full_no if occurrence == 0 else f"{full_no}（{occurrence + 1}）"
        blocks.append(
            ParsedBlock(
                level_code=current_kind,
                article_no=current_heading,
                content=content,
                full_no=unique_full_no,
                heading_path=_build_heading_path(chapter_head, section_head, current_heading),
                order_index=index,
                parent_index=parent_index,
            )
        )
        last_index[current_kind] = index
        buffer = []
        current_kind = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            # 空行不是层级边界，但保留为段落分隔，供"款"按自然段切分
            if buffer:
                buffer.append("")
            continue

        match = _CHAPTER_RE.match(line)
        if match:
            flush()
            chapter_head = line
            section_head = ""
            current_kind = "chapter"
            current_heading = match.group(1)
            buffer = [line[len(current_heading):].strip()]
            continue

        match = _SECTION_RE.match(line)
        if match:
            flush()
            section_head = line
            current_kind = "section"
            current_heading = match.group(1)
            buffer = [line[len(current_heading):].strip()]
            continue

        match = _ARTICLE_RE.match(line)
        if match:
            flush()
            current_kind = "article"
            context_kind = "article"
            current_heading = match.group(1)
            buffer = [match.group(2)]
            continue

        # 段落（款）可以出现在 条/款/项 任意上下文之后：
        # 实务里"（一）（二）"既跟在条后，也可能跟在项后重新起编。
        # 守卫若写成 {article, paragraph}，出现在项后的"（二）"会被漏掉，
        # 导致后续项的父级算错、完整编号重复，撞数据库唯一约束。
        if context_kind in {"article", "paragraph", "item"}:
            match = _PARAGRAPH_RE.match(line)
            if match:
                flush()
                current_kind = "paragraph"
                context_kind = "paragraph"
                current_heading = f"（{match.group(1)}）"
                buffer = [match.group(2)]
                continue

        if context_kind in {"article", "paragraph", "item"}:
            match = _ITEM_RE.match(line)
            if match:
                flush()
                current_kind = "item"
                context_kind = "item"
                current_heading = match.group(1)
                buffer = [match.group(2)]
                continue

        # 总局公告的"一、二、三、"分条。这类公告通篇没有"第X条"，
        # 因此必须放在 context_kind 守卫之外——没有上下文恰恰是它的典型特征。
        match = _CN_ITEM_RE.match(line)
        if match:
            flush()
            current_kind = "item"
            context_kind = "item"
            current_heading = match.group(1)
            buffer = [match.group(2)]
            continue

        # 普通正文行
        if current_kind is None:
            # 前言部分（制定目的、依据等），不属于任何层级，丢弃但不影响条文
            continue

        buffer.append(line)

    flush()
    return blocks


def _build_full_no(chapter: str, section: str, heading: str, level: str) -> str:
    """构造完整编号，如 "第一章 总则 第一条"。

    注意：款、项的编号在不同父级下会重复（每条都有"（一）"），
    所以它们的完整编号必须由调用方带着父级链拼出来，见 parser 里
    flush() 中的 _full_no_with_parents。这里只处理章、节、条。
    """

    parts = [p for p in (chapter, section) if p]
    # paragraph_base 是个内部哨兵值：表示"只取章、节前缀"，父级链由调用方拼
    if heading and level not in {"chapter", "section", "paragraph_base"}:
        parts.append(heading)
    return " ".join(parts)


def _build_heading_path(chapter: str, section: str, heading: str) -> str:
    parts = [p for p in (chapter, section) if p]
    if heading and heading not in parts:
        parts.append(heading)
    return "/".join(parts)


def _extract_title(lines: list[str], document_number: str | None) -> str:
    """标题识别。

    文件约定（采集脚本与清洗脚本都按这个格式写文件）：
        第一行 = 文号，第二行 = 标题；没有文号时第一行就是标题。

    所以判断依据是"首行到底是不是文号那一行"，而不是"往下找第一个像标题的短行"。

    为什么必须改：旧实现是"从第 2 行起找第一个不超过 80 字的行"。
    遇到没有文号的法规（法律、行政法规在官方库里就没有发文字号），
    正文里的"目　录""（一）土地使用权出让；"这类短行会被当成标题，
    整部法规于是以错误的标题入库。
    """

    if not lines:
        return ""

    first = lines[0].strip()
    has_header_line = bool(document_number) or bool(_DOC_NUMBER_LIKE_RE.match(first))

    if not has_header_line:
        # 首行不是文号那一行 → 按约定首行就是标题
        for line in lines[:6]:
            candidate = line.strip()
            if candidate:
                return candidate
        return ""

    # 首行是文号/主席令/国务院令 → 标题在它后面
    for line in lines[1:6]:
        candidate = line.strip()
        if not candidate or candidate == document_number:
            continue
        # 文号被页面拆成两行时，第二行（如"2018年第55号"）是文号的一部分，不是标题
        if document_number and candidate in document_number:
            continue
        if _CHAPTER_RE.match(candidate) or _SECTION_RE.match(candidate) or _ARTICLE_RE.match(candidate):
            continue
        return candidate
    return first


def parse_regulation(text: str, source_url: str | None = None) -> ParsedRegulation:
    """解析一份法规，返回元数据 + 条文列表。

    source_url 必传：没有来源留痕的法规不收（技术方案 10.3）。
    这里不硬性抛异常，而是标记 requires_review 让批量流水线能继续处理其他文件。
    """

    lines = text.splitlines() if text else []
    document_number, issuer = _match_document_number(text or "")
    title = _extract_title(lines, document_number)

    hierarchy_level = _detect_hierarchy(title, issuer)

    effective_date = _match_date(text or "", _EFFECTIVE_RE)
    expiry_date = _match_date(text or "", _REPEAL_DATE_RE)
    publish_date = None
    if document_number:
        normalized = _normalize_brackets(document_number)
        year_match = re.search(r"[〔\[［](\d{4})[〕\]］]", normalized)
        if not year_match:
            year_match = re.search(r"(\d{4})\s*年", normalized)
        if year_match:
            try:
                publish_date = datetime(int(year_match.group(1)), 1, 1, tzinfo=timezone.utc)
            except ValueError:
                publish_date = None

    blocks = split_articles(text or "")
    if not blocks:
        # 没有"第X条"也没有"一、二、"的通知：整篇就是一条规定。
        # 为什么要兜这一层：这类文件（如财税〔2018〕80 号那种一段话说清一件事的
        # 短通知）切不出条文，就会变成"入库了但检索不到"——用户问相关问题永远
        # 召不回它，而库里明明有。宁可给它一个"全文"条目，也不要让它凭空消失。
        body = _whole_text_body(lines, document_number, title)
        if len(body) >= MIN_WHOLE_TEXT_CHARS:
            blocks = [
                ParsedBlock(
                    level_code="article",
                    article_no="",
                    content=body,
                    full_no=WHOLE_TEXT_FULL_NO,
                    heading_path="",
                    order_index=0,
                )
            ]

    review_reasons: list[str] = []
    if not document_number:
        review_reasons.append("文号未识别，需人工确认（系统不编造文号）")
    if hierarchy_level is None:
        review_reasons.append("效力位阶未判定，需人工确认")
    if effective_date is None:
        review_reasons.append("生效日期缺失，需人工确认")
    if not source_url:
        review_reasons.append("缺少来源 URL 与获取时间，按合规要求不予收录")

    return ParsedRegulation(
        title=title,
        document_number=document_number,
        issuer=issuer,
        hierarchy_level=hierarchy_level or "normative_document",
        publish_date=publish_date,
        effective_date=effective_date,
        expiry_date=expiry_date,
        blocks=blocks,
        source_url=source_url,
        requires_review=bool(review_reasons),
        review_reasons=review_reasons,
    )

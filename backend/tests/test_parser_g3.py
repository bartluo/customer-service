"""法规解析器测试（条文切分 + 元数据识别）。

用纯内存样例文本，不依赖网络与真实法规数据，因此可重复执行。
覆盖技术方案 4.1 的硬要求：
  · 章 / 节 / 条 / 款 / 项 五级层级切分，引用单位精确到 条/款/项
  · 文号、发文机关、发布日期、施行日期识别
  · 抽不到施行日期时标待确认，绝不编造
"""

from __future__ import annotations

from app.knowledge.parser import (
    ParsedRegulation,
    parse_regulation,
    split_articles,
)


SAMPLE_VAT_NOTICE = """财政部 税务总局 海关总署公告2019年第39号

关于深化增值税改革有关政策的公告

为了深化增值税改革，优化增值税制度，统一增值税抵扣凭证，规范增值税纳税人
登记行为，特制定本公告。

一、增值税一般纳税人发生销售服务、无形资产或者不动产应税行为，不再缴纳预缴税款。

第一章 总则

第一条 本公告适用于中华人民共和国境内（以下简称境内）增值税纳税人。

第二条 增值税一般纳税人适用税率13%的项目包括销售货物、加工修理修配劳务、有形动产租赁服务、
进口货物，以及其他情形，具体范围见附件一。

（一）纳税人发生适用13%税率的销售行为，适用13%税率。

（二）纳税人兼营适用不同税率的项目，应当分别核算适用税率的销售行为。

2.下列情形适用9%税率：

第三章 附则

第二十五条 本公告自2019年4月1日起施行。

第二十六条 财政部 税务总局关于深化增值税改革有关问题的公告〔2019〕38号同时废止。
"""


def test_split_articles_detects_all_levels() -> None:
    """章、节、条、款、项五级都要能识别。"""

    blocks = split_articles(SAMPLE_VAT_NOTICE)
    levels = [block.level_code for block in blocks]

    assert "chapter" in levels
    assert "article" in levels
    assert "paragraph" in levels
    assert "item" in levels


def test_split_articles_keeps_article_number_and_content() -> None:
    """切出的条文要带编号和正文，编号不能丢。"""

    blocks = split_articles(SAMPLE_VAT_NOTICE)
    articles = [b for b in blocks if b.level_code == "article"]

    assert len(articles) >= 3
    assert articles[0].article_no == "第一条"
    assert "本公告适用于中华人民共和国境内" in articles[0].content
    assert articles[-1].article_no == "第二十六条"
    # 附则里的施行日期条文必须被保留，不能丢
    assert any("自2019年4月1日起施行" in b.content for b in articles)


def test_split_articles_builds_full_no_path() -> None:
    """full_no 要能表达"第一章/第一条"这样的层级定位。"""

    blocks = split_articles(SAMPLE_VAT_NOTICE)
    first_article = next(b for b in blocks if b.article_no == "第一条")
    assert "第一章" in first_article.full_no
    assert "第一条" in first_article.full_no


def test_split_articles_handles_empty_input() -> None:
    """空输入不能崩，返回空列表。"""

    assert split_articles("") == []


def test_parse_regulation_extracts_metadata() -> None:
    """文号、发文机关、施行日期识别准确。"""

    parsed: ParsedRegulation = parse_regulation(SAMPLE_VAT_NOTICE)

    assert parsed.title == "关于深化增值税改革有关政策的公告"
    assert parsed.document_number == "财政部 税务总局 海关总署公告2019年第39号"
    assert parsed.issuer == "财政部 税务总局 海关总署"
    assert parsed.effective_date is not None
    assert parsed.effective_date.year == 2019
    assert parsed.effective_date.month == 4
    assert parsed.effective_date.day == 1


def test_parse_regulation_detects_hierarchy_level() -> None:
    """位阶判定。公告属于规范性文件。"""

    parsed = parse_regulation(SAMPLE_VAT_NOTICE)
    assert parsed.hierarchy_level == "normative_document"


def test_parse_regulation_does_not_fabricate_missing_fields() -> None:
    """抽不到施行日期时置 None 并转人工复核，绝不猜。"""

    text = """财政部 税务总局公告2019年第39号

关于深化增值税改革有关政策的公告

第一条 本公告适用于中华人民共和国境内增值税纳税人。
"""
    parsed = parse_regulation(text)

    assert parsed.effective_date is None
    assert parsed.requires_review is True
    assert "生效日期" in parsed.review_reasons[0]


def test_parse_regulation_requires_source() -> None:
    """技术方案 10.3：没有来源留痕的法规一律不收。"""

    parsed = parse_regulation(SAMPLE_VAT_NOTICE, source_url=None)
    assert parsed.requires_review is True
    assert any("来源" in reason for reason in parsed.review_reasons)


def test_parse_regulation_keeps_unmatched_fields_for_review() -> None:
    """识别不出文号时不编造，标为待复核。"""

    text = """关于印发某某某规定的通知

第一条 本规定自2020年1月1日起施行。
"""
    parsed = parse_regulation(text, source_url="https://example.gov.cn/x")

    assert parsed.document_number is None
    assert parsed.requires_review is True
    assert any("文号" in reason for reason in parsed.review_reasons)


DUPLICATE_PARAGRAPH_NUMBERS = """财政部 税务总局公告2022年第1号

关于增值税政策事项的公告

第一章 总则

第一条 增值税一般纳税人适用税率13%。

（一）纳税人发生适用13%税率的销售行为，适用13%税率。

（二）纳税人兼营不同税率项目，分别核算。

第二条 增值税小规模纳税人适用3%征收率。

（一）适用3%征收率的应税销售收入，减按1%征收。

（二）适用3%预征率的应税劳务，预征率由3%减按1%。
"""


def test_duplicate_paragraph_numbers_get_unique_full_no() -> None:
    """回归测试：多条条文下都有"（一）"时，完整编号必须各不相同。

    之前 full_no 只用款自身的编号，导致同一法规里多个"（一）"完全相同，
    导入时撞 uq_article_reg_full_no 唯一约束，34 份真实法规里有 9 份因此失败。
    完整编号必须带父级链："第一章 总则 第一条 （一）"。
    """

    blocks = split_articles(DUPLICATE_PARAGRAPH_NUMBERS)
    paragraphs = [b for b in blocks if b.level_code == "paragraph"]

    assert len(paragraphs) == 4
    full_nos = [b.full_no for b in paragraphs]
    assert len(full_nos) == len(set(full_nos)), f"款编号重复：{full_nos}"
    # 每个完整编号都应包含其所属的条
    assert all("第一条" in no or "第二条" in no for no in full_nos)


def test_duplicate_item_numbers_get_unique_full_no() -> None:
    """项编号同样会重复，完整编号必须带父级链。"""

    text = """财政部 税务总局公告2022年第1号

关于增值税政策事项的公告

第一章 总则

第一条 下列情形适用9%税率：

（一）交通运输服务。

1.铁路运输服务。

2.公路运输服务。

（二）邮政服务。

1.邮政普遍服务。

2.邮政特殊服务。
"""
    blocks = split_articles(text)
    items = [b for b in blocks if b.level_code == "item"]
    full_nos = [b.full_no for b in items]
    assert len(full_nos) == len(set(full_nos)), f"项编号重复：{full_nos}"


def test_article_full_no_is_still_readable() -> None:
    """条级完整编号保持"第一章/第一条"这种可读形式。"""

    blocks = split_articles(DUPLICATE_PARAGRAPH_NUMBERS)
    articles = [b for b in blocks if b.level_code == "article"]
    assert articles[0].full_no == "第一章 总则 第一条"


# 官方法规库网页的实际排版：条号单独成一行，正文另起一行。
# 这批数据里企业所得税法、税收征管法等 10 余部都是这个格式，
# 若解析器只认"第一条 正文"同行，条级会全部丢失。
ARTICLE_ON_OWN_LINE = """中华人民共和国主席令第六十三号

中华人民共和国企业所得税法

第一章 总 则

第一条
在中华人民共和国境内,企业和其他取得收入的组织(以下统称企业)为企业所得税的纳税人。

第二条
企业分为居民企业和非居民企业。
本法所称居民企业,是指依法在中国境内成立的企业。

第三条
居民企业应当就其来源于中国境内、境外的所得缴纳企业所得税。
"""


def test_article_number_on_own_line_is_recognized() -> None:
    """条号单独成行时也要切出条文，正文取后续行。"""

    blocks = split_articles(ARTICLE_ON_OWN_LINE)
    articles = [b for b in blocks if b.level_code == "article"]

    assert len(articles) == 3
    assert articles[0].article_no == "第一条"
    assert "企业所得税的纳税人" in articles[0].content
    assert articles[1].article_no == "第二条"
    assert "居民企业和非居民企业" in articles[1].content


def test_article_on_own_line_keeps_multiple_paragraphs() -> None:
    """条内多个自然段（款）仍要归到同一条下。"""

    blocks = split_articles(ARTICLE_ON_OWN_LINE)
    articles = [b for b in blocks if b.level_code == "article"]
    # 第二条有两段，应都留在第二条里
    assert articles[1].content.count("本法所称居民企业") == 1
    assert "非居民企业" in articles[1].content


def test_law_with_own_line_articles_parses_metadata() -> None:
    """整部法律的元数据与条级都要正确（真实数据回归）。"""

    parsed = parse_regulation(ARTICLE_ON_OWN_LINE, source_url="https://fgk.chinatax.gov.cn/x")
    assert parsed.title == "中华人民共和国企业所得税法"
    assert parsed.hierarchy_level == "law"
    assert len(parsed.articles) == 3


# 总局公告 / 通知的通用体例：用"一、二、三、"分条，没有"第X条"。
# 首批 34 份真实法规里有 14 份是这个体例，不识别就整份切不出条文。
CN_NUMBERED_NOTICE = """财政部 税务总局公告2023年第19号

财政部 税务总局关于增值税小规模纳税人减免增值税政策的公告

为进一步支持小微企业和个体工商户发展,现将延续小规模纳税人增值税减免政策公告如下:

一、对月销售额10万元以下(含本数)的增值税小规模纳税人,免征增值税。

二、增值税小规模纳税人适用3%征收率的应税销售收入,减按1%征收率征收增值税。

三、本公告执行至2027年12月31日。

特此公告。
"""


def test_cn_numbered_notice_splits_into_items() -> None:
    """"一、二、三、"必须被识别为可引用单元。"""

    blocks = split_articles(CN_NUMBERED_NOTICE)
    items = [b for b in blocks if b.level_code == "item"]

    assert len(items) == 3
    assert items[0].article_no == "一、"
    assert "免征增值税" in items[0].content
    assert "减按1%征收率" in items[1].content
    assert "2027年12月31日" in items[2].content


def test_cn_numbered_notice_has_unique_full_no() -> None:
    """顶层分条的完整编号不能重复。"""

    blocks = split_articles(CN_NUMBERED_NOTICE)
    items = [b for b in blocks if b.level_code == "item"]
    full_nos = [b.full_no for b in items]
    assert len(full_nos) == len(set(full_nos)), full_nos


def test_cn_numbered_notice_metadata() -> None:
    """文号、标题、位阶都要识别出来。"""

    parsed = parse_regulation(CN_NUMBERED_NOTICE, source_url="https://fgk.chinatax.gov.cn/x")
    assert parsed.document_number == "财政部 税务总局公告2023年第19号"
    assert parsed.issuer == "财政部 税务总局"
    assert parsed.hierarchy_level == "normative_document"
    assert parsed.effective_date is None  # 没写施行日期就是没写，不猜


# 没有文号的法规：官方库里的法律、行政法规就是这样（发文字号那一栏是空的）。
# 采集脚本按"首行=文号、次行=标题；无文号时首行就是标题"的约定写文件。
N0_DOC_NO_NUMBER = """中华人民共和国资源税法

目　　录
第一章　总　　则
第二章　计税依据和应纳税额

第一章　总　　则

第一条　在中华人民共和国领域和中华人民共和国管辖的其他海域开发应税资源的
单位和个人为资源税的纳税人，应当依照本法规定缴纳资源税。

第二条
应税资源的具体范围，由本法所附《资源税税目税率表》（以下称《税目税率表》）确定。
"""

N0_DOC_WITH_ORDER = """中华人民共和国主席令第六十三号

中华人民共和国企业所得税法

第一章 总 则

第一条 在中华人民共和国境内，企业和其他取得收入的组织为企业所得税的纳税人。
"""


def test_title_without_document_number_uses_first_line() -> None:
    """无文号时首行就是标题，不能被正文里的"目　录"顶掉。

    回归背景：首行不是文号而又去"往下找第一个短行"，会把目录行、
    分项行当成标题，整部法规以错误的标题入库。
    """

    parsed = parse_regulation(N0_DOC_NO_NUMBER, source_url="https://fgk.chinatax.gov.cn/x")

    assert parsed.document_number is None
    assert parsed.title == "中华人民共和国资源税法"
    assert parsed.hierarchy_level == "law"
    assert len(parsed.articles) == 2


def test_title_follows_presidential_order_line() -> None:
    """主席令这类文号行后面的一行才是标题。"""

    parsed = parse_regulation(N0_DOC_WITH_ORDER, source_url="https://fgk.chinatax.gov.cn/x")

    assert parsed.title == "中华人民共和国企业所得税法"
    assert parsed.hierarchy_level == "law"


# 页面把文号拆成两行的体例（真实数据里出现过：
# "国家税务总局公告" 一行、"2018年第55号" 下一行）
SPLIT_DOC_NUMBER = """国家税务总局公告
2018年第55号
国家税务总局关于将个人所得税《税收完税证明》调整为《纳税记录》有关事项的公告

一、从2019年1月1日起，税务机关不再开具《税收完税证明》，调整为开具《纳税记录》。
二、纳税人可以通过电子税务局、手机APP申请开具。
"""


def test_document_number_split_across_two_lines() -> None:
    """文号被页面拆成两行时，文号要认出来，标题不能被当成"国家税务总局公告"。"""

    parsed = parse_regulation(SPLIT_DOC_NUMBER, source_url="https://fgk.chinatax.gov.cn/x")

    assert parsed.document_number == "国家税务总局公告2018年第55号"
    assert parsed.issuer == "国家税务总局"
    assert parsed.title == "国家税务总局关于将个人所得税《税收完税证明》调整为《纳税记录》有关事项的公告"
    assert parsed.hierarchy_level == "normative_document"


# 一段话说清一件事的短通知：没有"第X条"，也没有"一、二、"
WHOLE_TEXT_NOTICE = """财税〔2018〕80号
财政部 国家税务总局关于增值税期末留抵退税有关城市维护建设税教育费附加和地方教育附加政策的通知

为保证增值税期末留抵退税政策有效落实，现就留抵退税涉及的城市维护建设税、教育费附加和地方教育附加问题通知如下：
对实行增值税期末留抵退税的纳税人，允许其从城市维护建设税、教育费附加和地方教育附加的计税依据中扣除退还的增值税税额。
本通知自发布之日起施行。
"""


def test_whole_text_notice_becomes_one_article() -> None:
    """切不出条文的短通知要整篇算一条，不能"入库了却检索不到"。

    回归背景：财税〔2018〕80 号这类通知只有一段话，切分器找不到任何层级标记，
    结果是 0 条条文——库里明明有这份文件，用户却永远召不回它。
    """

    parsed = parse_regulation(WHOLE_TEXT_NOTICE, source_url="https://fgk.chinatax.gov.cn/x")

    assert parsed.document_number == "财税〔2018〕80号"
    assert len(parsed.blocks) == 1
    block = parsed.blocks[0]
    assert block.level_code == "article"
    assert block.full_no == "全文"
    assert "扣除退还的增值税税额" in block.content
    # 文号行与标题行不能混进正文
    assert "财税〔2018〕80号" not in block.content


def test_too_short_text_is_not_padded_into_an_article() -> None:
    """页脚残留这类短内容不能硬凑成条文。"""

    parsed = parse_regulation("某文件\n短。", source_url="https://fgk.chinatax.gov.cn/x")
    assert parsed.blocks == []

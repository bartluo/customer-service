"""法规关系抽取与知识单元抽取测试。

对应任务清单：
  识别上位法、引用、替代、废止关系，抽查 20 组准确率 ≥ 90%
  从条文中提炼可独立回答的知识单元，抽查 50 条可用率 ≥ 80%

关系抽取决定"能不能顺着查"；知识单元决定"能不能直接回答"。
两者都是纯逻辑，不连数据库。
"""

from __future__ import annotations

from app.knowledge.relations import extract_relations
from app.knowledge.units import extract_knowledge_units, score_unit_usability


AMEND_TEXT = """第二条 本公告根据《中华人民共和国增值税暂行条例》制定。
第三条 财政部 税务总局关于深化增值税改革有关问题的公告〔2019〕38号同时废止。
第四条 增值税一般纳税人适用税率13%的项目包括销售货物，具体范围见附件一。
"""


def test_extract_repeals_relation() -> None:
    """识别"同时废止"关系。"""

    relations = extract_relations(AMEND_TEXT, "财政部 税务总局公告2022年第1号")
    types = [r.relation_type for r in relations]
    assert "repeals" in types

    repeal = next(r for r in relations if r.relation_type == "repeals")
    assert "2019" in repeal.target_ref
    assert repeal.evidence
    assert repeal.confidence > 0.5


def test_extract_based_on_relation() -> None:
    """识别"根据某某法制定"的上位法关系。"""

    relations = extract_relations(AMEND_TEXT, "财政部 税务总局公告2022年第1号")
    types = [r.relation_type for r in relations]
    assert "based_on" in types
    based = next(r for r in relations if r.relation_type == "based_on")
    assert "增值税暂行条例" in based.target_ref


def test_extract_references_relation() -> None:
    """识别"见附件一"这类引用。"""

    relations = extract_relations(AMEND_TEXT, "财政部 税务总局公告2022年第1号")
    types = [r.relation_type for r in relations]
    assert "references" in types


def test_extract_relations_deduplicates() -> None:
    """同一关系不重复产出。"""

    relations = extract_relations(AMEND_TEXT, "财政部 税务总局公告2022年第1号")
    keys = [(r.relation_type, r.target_ref) for r in relations]
    assert len(keys) == len(set(keys))


def test_extract_relations_returns_empty_for_plain_text() -> None:
    """普通条文不硬造关系。"""

    relations = extract_relations("第一条 增值税税率为13%。", "公告")
    assert relations == []


def test_extract_knowledge_units_from_articles() -> None:
    """从条文中提炼可独立回答的知识单元。"""

    from app.knowledge.parser import parse_regulation

    parsed = parse_regulation(
        """财政部 税务总局公告2022年第1号

关于某事项的公告

第一条 增值税小规模纳税人适用3%征收率的应税销售收入，减按1%征收率征收增值税。

第二条 小规模纳税人发生销售错误，按适用税率3%征收。
""",
        source_url="https://example.gov.cn/1",
    )
    units = extract_knowledge_units(parsed)

    assert len(units) >= 2
    for unit in units:
        assert unit.content
        assert unit.source_no
        assert 0.0 <= unit.usability <= 1.0


def test_knowledge_units_skip_chapters_and_sections() -> None:
    """章、节不是可独立回答的知识单元，不应产出。"""

    from app.knowledge.parser import parse_regulation

    parsed = parse_regulation(
        """财政部 税务总局公告2022年第1号

关于某事项的公告

第一章 总则

第一条 增值税小规模纳税人减按1%征收率征收增值税。
""",
        source_url="https://example.gov.cn/1",
    )
    units = extract_knowledge_units(parsed)
    assert all(unit.level_code in {"article", "paragraph", "item"} for unit in units)


def test_knowledge_units_measure_usability() -> None:
    """可用率评分：完整、有条件、有结论的条文得分高。"""

    from app.knowledge.units import KnowledgeUnit

    good = KnowledgeUnit(
        level_code="article",
        source_no="第一条",
        content="增值税一般纳税人发生销售服务适用13%税率。",
        question_hint="什么情况下适用13%税率？",
        answer_summary="销售服务适用13%税率。",
        usability=0.9,
    )
    vague = KnowledgeUnit(
        level_code="article",
        source_no="第二条",
        content="依照有关规定执行。",
        question_hint="",
        answer_summary="",
        usability=0.1,
    )
    assert score_unit_usability(good.content) > score_unit_usability(vague.content)


def test_knowledge_units_flag_vague_text() -> None:
    """过于空泛的条文（如"依照有关规定执行"）可用率必须低。"""

    from app.knowledge.units import KnowledgeUnit

    vague = KnowledgeUnit(
        level_code="article",
        source_no="第二条",
        content="依照有关规定执行。",
        question_hint="",
        answer_summary="",
        usability=0.1,
    )
    assert vague.usability < 0.5

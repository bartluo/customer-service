"""答案模板与问答主链路验收。

这一层的验收重点不是"答案好不好看"，而是两条硬约束：
  · 必填段落缺失必须判为生成失败——不能给一个看起来正常、实际少一半内容的答案；
  · 免责段由模板渲染，100% 出现，不依赖任何调用方传值。
"""

from __future__ import annotations

import pathlib

import pytest

from app.reasoning.pipeline import AnswerPipeline, classify_intent
from app.reasoning.templates import TemplateError, load_templates, render

REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "domains" / "finance_tax" / "templates.yaml"


@pytest.fixture(scope="module")
def templates():
    return load_templates(TEMPLATES)


# ---------------------------------------------------------------------------
# 答案模板引擎
# ---------------------------------------------------------------------------


def test_templates_load(templates) -> None:
    assert "finance_tax.answer" in templates
    assert "finance_tax.clarification" in templates
    answer = templates["finance_tax.answer"]
    assert answer.required_keys() == ("judgement", "basis", "disclaimer")


def test_six_sections_in_order(templates) -> None:
    """六段式顺序固定：判定 → 依据 → 计算 → 步骤 → 风险 → 说明。"""

    keys = [section.key for section in templates["finance_tax.answer"].sections]
    assert keys == ["judgement", "basis", "calculation", "steps", "risks", "disclaimer"]


def test_missing_required_section_fails(templates) -> None:
    """必填段缺失 → 生成失败，而不是悄悄少一段。"""

    rendered = render(templates["finance_tax.answer"], {"judgement": "结论"})
    assert rendered.ok is False
    assert "basis" in rendered.missing_required
    assert "生成失败" in rendered.failure_reason


def test_empty_required_section_also_fails(templates) -> None:
    """必填段传了空值同样算缺失——空段在界面上看起来像"这段没内容"。"""

    rendered = render(templates["finance_tax.answer"], {"judgement": "", "basis": []})
    assert rendered.ok is False
    assert set(rendered.missing_required) == {"judgement", "basis"}


def test_disclaimer_always_rendered_from_template(templates) -> None:
    """免责段由模板自己填：调用方不给也要出现，给别的内容也不认。"""

    rendered = render(
        templates["finance_tax.answer"],
        {"judgement": "结论", "basis": [{"text": "某条款"}], "disclaimer": "我自己写的免责"},
    )
    disclaimer = next(item for item in rendered.sections if item.key == "disclaimer")
    assert "以主管税务机关认定为准" in str(disclaimer.content)
    assert "我自己写的免责" not in str(disclaimer.content)


def test_optional_sections_are_skipped_when_absent(templates) -> None:
    rendered = render(templates["finance_tax.answer"], {"judgement": "结论", "basis": []})
    # basis 为空会失败，所以这里用一个非空的 basis 再看选填段
    rendered = render(
        templates["finance_tax.answer"], {"judgement": "结论", "basis": [{"text": "某条款"}]}
    )
    assert rendered.ok is True
    keys = [item.key for item in rendered.sections]
    assert "calculation" not in keys
    assert "steps" not in keys


def test_structured_output_shape(templates) -> None:
    """输出必须是结构化 JSON：前端按字段渲染，不解析自然语言。"""

    rendered = render(
        templates["finance_tax.answer"],
        {
            "judgement": "结论",
            "basis": [{"text": "增值税法 第八条"}],
            "calculation": [
                {
                    "title": "应纳税额",
                    "formula": "销售额 × 征收率",
                    "substitution": "100.00 × 3%",
                    "result": "3.00",
                }
            ],
        },
    )
    payload = rendered.to_dict()
    assert payload["ok"] is True
    section = next(item for item in payload["sections"] if item["key"] == "calculation")
    assert section["renderer"] == "formula_steps"
    assert section["content"][0]["result"] == "3.00"


def test_bad_template_file_is_rejected(tmp_path) -> None:
    """模板写错（必填段没有、固定文案缺 text）必须在加载期报错。"""

    broken = tmp_path / "bad.yaml"
    broken.write_text(
        """
domain_id: finance_tax
templates:
  - template_id: x
    sections:
      - key: a
        title: A
        renderer: text
""",
        encoding="utf-8",
    )
    with pytest.raises(TemplateError):
        load_templates(broken)

    broken.write_text(
        """
domain_id: finance_tax
templates:
  - template_id: x
    sections:
      - key: a
        title: A
        required: true
        renderer: text
      - key: d
        title: D
        required: true
        renderer: fixed_text
""",
        encoding="utf-8",
    )
    with pytest.raises(TemplateError):
        load_templates(broken)


# ---------------------------------------------------------------------------
# 问答主链路
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("我这笔业务要交多少税", "calculation"),
        ("申报流程是什么，需要什么材料", "how_to"),
        ("被税务机关查了怎么办", "inspection"),
        ("能不能享受小微优惠", "policy_query"),
        ("怎么才能少交税", "planning"),
    ],
)
def test_classify_intent(question: str, expected: str) -> None:
    assert classify_intent(question) == expected


def test_pipeline_refuses_without_any_citation() -> None:
    """检索不到依据时必须拒答，而不是编一个答案。

    这里用的是空的测试库：没有任何法规，正好验证拒答路径。
    """

    import os

    import sqlalchemy as sa
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.database.base import Base

    server_url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/customer_service_test_answer"
    server = create_engine(server_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :n"),
            {"n": "customer_service_test_answer"},
        ).scalar()
        if not exists:
            conn.execute(sa.text('create database "customer_service_test_answer"'))
    server.dispose()

    engine = create_engine(server_url)
    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    session = sessionmaker(bind=engine)()
    try:
        pipeline = AnswerPipeline(session)
        # 要素给全（身份 + 期间 + 业务 + 含税口径），这样才会走到"没有依据就拒答"，
        # 而不是先在"要素不全"那一步转去追问。
        result = pipeline.answer("我是小规模纳税人，上个月卖了10万元含税货物，怎么交税")
        assert result.refused is True
        assert result.refusal_reason
        # 拒答也要按模板输出，且免责段照样在
        assert result.answer is not None
        assert any(item.key == "disclaimer" for item in result.answer.sections)
    finally:
        session.close()
        engine.dispose()


def test_pipeline_asks_before_answering_when_slots_missing() -> None:
    """要素不全时走追问模板，不硬答。"""

    import os

    import sqlalchemy as sa
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.database.base import Base

    database = "customer_service_test_ask"
    server_url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/" + database
    server = create_engine(server_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :n"), {"n": database}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{database}"'))
    server.dispose()

    engine = create_engine(server_url)
    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    session = sessionmaker(bind=engine)()
    try:
        pipeline = AnswerPipeline(session)
        result = pipeline.answer("怎么办")
        assert result.refused is False
        assert result.template_id == "finance_tax.clarification"
        assert "需要您补充" in result.to_text() or "确认" in result.to_text()
    finally:
        session.close()
        engine.dispose()


def test_inferred_identity_is_not_stated_as_fact() -> None:
    """推测出来的身份不能写成"您的身份"——用户会以为系统已经核实过。

    回归背景：用户只说"我开了个小卖部"，系统却在判定里写
    "您的身份：个体工商户"，把推测当成了事实。

    （2026-10-02 补充：如果用户**自己说清了**主体，答案里连"推测"这句都不写——
    见 test_stated_subject_is_not_restated。）
    """

    from app.reasoning.pipeline import AnswerPipeline as Pipeline
    from app.reasoning.templates import load_templates as _load

    # 直接验组装逻辑，不依赖数据库
    from app.reasoning.facts import ExtractedFacts
    from app.reasoning.applicability import ApplicabilityReport, PolicyVerdict, Verdict

    facts = ExtractedFacts(raw_text="我开了个小卖部")
    facts.taxpayer_type = "个体工商户"
    facts.taxpayer_type_confirmed = False
    facts.taxpayer_type_stated = False

    report = ApplicabilityReport()
    report.add(
        PolicyVerdict(title="某公告", citation="某公告 一、", verdict=Verdict.APPLICABLE)
    )

    contents = Pipeline.__dict__["_build_contents"](None, facts, report, None)
    assert "尚未确认" in contents["judgement"]
    assert "您的身份：个体工商户" not in contents["judgement"]


def test_stated_subject_is_not_restated() -> None:
    """用户自己说了主体就别再复述，直接给答案。

    回归背景（原话）："回答过于刻意……问题已经说了小微企业，
    那就直接回答小微企业的税收优惠政策"。
    """

    from app.reasoning.pipeline import AnswerPipeline as Pipeline
    from app.reasoning.facts import ExtractedFacts
    from app.reasoning.applicability import ApplicabilityReport, PolicyVerdict, Verdict

    facts = ExtractedFacts(raw_text="小微企业有什么税收优惠")
    facts.taxpayer_type = "小型微利企业"
    facts.taxpayer_type_confirmed = False  # 俗称与税法口径不同，仍需在依据里说明
    facts.taxpayer_type_stated = True

    report = ApplicabilityReport()
    report.add(
        PolicyVerdict(
            title="财政部 税务总局公告2023年第12号",
            citation="财政部 税务总局公告2023年第12号 四、",
            verdict=Verdict.APPLICABLE,
            content="对小型微利企业年应纳税所得额不超过300万元的部分，减按25%计入应纳税所得额。",
        )
    )

    judgement = Pipeline.__dict__["_build_contents"](None, facts, report, None)["judgement"]

    assert "尚未确认" not in judgement  # 不复述、不加前缀
    assert "按您描述的情形" not in judgement
    assert judgement.startswith("可以享受的税收优惠主要有")  # 直接就是答案


def test_subject_missing_asks_before_answering() -> None:
    """问题里没说主体、又在问"优惠"，先问主体。

    与旧口径的冲突（ADR-0017 §5 曾定"'增值税怎么交'直接答"）已在
    ADR-0020 补记中记录，以 2026-10-02 的要求为准。
    """

    from app.reasoning.pipeline import AnswerPipeline as Pipeline
    from app.reasoning.facts import ExtractedFacts

    # 没说主体 + 问优惠 → 先问主体

    missing = ExtractedFacts(raw_text="有哪些税收优惠")
    missing.questions = ["您的企业类型与纳税人身份分别是什么？"]
    assert Pipeline._needs_clarification("policy_query", missing, missing.raw_text) is True

    # 问题本身是条文性的（谁问答案都一样）→ 不追问
    definitional = ExtractedFacts(raw_text="增值税的税率是多少")
    definitional.questions = ["您的企业类型与纳税人身份分别是什么？"]
    assert Pipeline._needs_clarification("policy_query", definitional, definitional.raw_text) is False

    # 说了主体 + 问优惠 → 直接答，不追问
    stated = ExtractedFacts(raw_text="小微企业有什么税收优惠")
    stated.taxpayer_type = "小型微利企业"
    stated.questions = ["您的情况是…吗？"]
    assert Pipeline._needs_clarification("policy_query", stated, stated.raw_text) is False


def test_benefit_sentences_beat_procedural_ones() -> None:
    """问"有什么优惠"时，写着比例的条文要排在"怎么办手续"的前面。

    回归背景（原话）："你现在给的答案没有给出答案，而是给出了条文。
    应该是直接给出税收优惠的详细信息，附带参考依据。"
    实测：检索把"通过填写纳税申报表即可享受"排前面，
    真正写着"减按25%…按20%的税率""减半征收"的第二条第三条排后面。
    """

    from app.reasoning.pipeline import _is_substantive, _point_text, _substance_score

    benefit = (
        "对小型微利企业减按25%计算应纳税所得额,按20%的税率缴纳企业所得税政策,"
        "延续执行至2027年12月31日。"
    )
    procedural = (
        "小型微利企业应准确填报基础信息，信息系统将为小型微利企业智能预填优惠项目、"
        "自动计算减免税额。"
    )

    assert _substance_score(benefit) > _substance_score(procedural)
    assert _is_substantive(benefit) is True
    assert _is_substantive(procedural) is False


def test_double_tax_free_substring_is_not_a_benefit_marker() -> None:
    """中文没有词边界：不能把"减免税额"里的"免税"当成免税优惠。

    这个坑实际发生过——加了子串匹配以后，"自动计算减免税额"这条程序性条款
    靠"免税"两个字拿到高分，重新爬到答案第一位。
    """

    from app.reasoning.pipeline import _substance_score

    procedural = "…累计情况，计算减免税额。"
    assert _substance_score(procedural) < 5

    real = "小规模纳税人发生应税交易,销售额未达到起征点的,免征增值税。"
    assert _substance_score(real) >= 5


def test_pick_the_most_informative_sentence() -> None:
    """一段条文里挑"最该给用户看"的那句，而不是无脑取第一句。"""

    from app.reasoning.pipeline import _point_text

    content = (
        "本公告所称小型微利企业,是指从事国家非限制和禁止行业的企业。"
        "且同时符合年度应纳税所得额不超过300万元、从业人数不超过300人、"
        "资产总额不超过5000万元等三个条件的企业。"
    )
    point = _point_text(content)
    assert "不超过300万元" in point or "从业人数不超过300人" in point


def test_judgement_gives_the_answer_not_a_file_list() -> None:
    """「情形判定」必须是答案，不能是一串文件名。

    回归背景：用户提问后拿到的判定段长这样——
        "可以适用：国家税务总局公告2023年第6号 三、；财政部 税务总局公告2023年第12号 四、"
    用户的原话是："应该是你给我答案，完了备注说明依据哪些文件"。
    也就是说：**先讲政策说了什么（答案），文号只能作为出处备注**。
    """

    from app.reasoning.pipeline import AnswerPipeline as Pipeline
    from app.reasoning.facts import ExtractedFacts
    from app.reasoning.applicability import ApplicabilityReport, PolicyVerdict, Verdict

    facts = ExtractedFacts(raw_text="小微企业有什么税收优惠")
    facts.taxpayer_type = "小型微利企业"
    facts.taxpayer_type_confirmed = True

    report = ApplicabilityReport()
    report.add(
        PolicyVerdict(
            title="财政部 税务总局公告2023年第12号",
            citation="财政部 税务总局公告2023年第12号 四、",
            verdict=Verdict.APPLICABLE,
            content="对小型微利企业年应纳税所得额不超过300万元的部分，减按25%计入应纳税所得额，"
            "按20%的税率缴纳企业所得税。本条自2023年1月1日起执行。",
        )
    )

    judgement = Pipeline.__dict__["_build_contents"](None, facts, report, None)["judgement"]

    # ① 政策说了什么，要出现在答案里（这是"答案"）
    assert "按20%的税率缴纳企业所得税" in judgement
    # ② 文号只作为出处备注，且明确标成"出处"
    assert "（出处：财政部 税务总局公告2023年第12号 四、）" in judgement
    # ③ 旧写法（把文件清单当答案）不能回来
    assert "可以适用：" not in judgement
    # ④ 只取条文要点，不整段照搬
    assert "自2023年1月1日起执行" not in judgement


def test_risks_never_contain_blank_bullets() -> None:
    """风险提示里不能出现空项目符号。

    空白条目比没有这一栏更糟：用户会以为"这里有内容但没显示出来"。
    实测出现过三条空的项目符号（原因是 reason 为空串）。
    """

    from app.reasoning.pipeline import AnswerPipeline as Pipeline
    from app.reasoning.facts import ExtractedFacts
    from app.reasoning.applicability import (
        ApplicabilityReport,
        ConditionResult,
        PolicyVerdict,
        Verdict,
    )

    facts = ExtractedFacts(raw_text="某问题")
    verdict = PolicyVerdict(
        title="某公告",
        citation="某公告 一、",
        verdict=Verdict.NEED_CONFIRM,
        conditions=[ConditionResult(name="主体", passed=None, reason="")],
    )
    report = ApplicabilityReport()
    report.add(verdict)

    contents = Pipeline.__dict__["_build_contents"](None, facts, report, None)
    for item in contents.get("risks", []):
        assert item.strip(), f"风险提示里有空条目：{contents['risks']!r}"

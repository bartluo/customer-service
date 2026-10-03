"""验证层测试。

四类验证器各测一遍，外加失败处理的三条路径（修正 / 重新生成 / 降级）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database.base import Base
from app.services.ingest.importer import DocumentInput, import_documents
from app.verification import (
    AnswerVerifier,
    CalculationVerifier,
    CitationVerifier,
    ComplianceVerifier,
    TemporalVerifier,
    VerificationInput,
)
from app.verification.result import Severity

TEST_DB = "customer_service_test_verify"

DOC = """财政部 税务总局公告2020年第23号

关于某事项的公告

第一条 小规模纳税人发生应税交易，适用征收率百分之三。

第二条 本公告自2020年3月1日起施行。
"""


@pytest.fixture(scope="module")
def factory():
    server_url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    server = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :n"), {"n": TEST_DB}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{TEST_DB}"'))
    server.dispose()

    url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/" + TEST_DB
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture()
def session(factory):
    db = factory()
    import_documents(
        db,
        [
            DocumentInput(
                filename="notice.txt",
                text=DOC,
                source_url="https://fgk.chinatax.gov.cn/test",
            )
        ],
    )
    try:
        yield db
    finally:
        db.close()


def _citation(**overrides) -> dict:
    base = {
        "document_number": "财政部 税务总局公告2020年第23号",
        "full_no": "第一条",
        "regulation_title": "关于某事项的公告",
        "effect_status": "effective",
        "content": "小规模纳税人发生应税交易，适用征收率百分之三。",
    }
    base.update(overrides)
    return base


def _answer_sections(**overrides) -> dict:
    base = {
        "judgement": "小规模纳税人适用 3% 征收率。",
        "basis": [{"text": "财政部 税务总局公告2020年第23号 第一条"}],
        "disclaimer": "本回答依据现行有效政策生成，具体口径以主管税务机关认定为准。",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 引用验证器
# ---------------------------------------------------------------------------


def test_citation_verifier_accepts_real_citation(session) -> None:
    result = CitationVerifier(session).verify(
        VerificationInput(citations=[_citation()], sections=_answer_sections())
    )
    assert result.passed is True


def test_citation_verifier_blocks_fake_document_number(session) -> None:
    """编造文号必须被拦——引用错误是"编造依据"，比算错更严重。"""

    result = CitationVerifier(session).verify(
        VerificationInput(
            citations=[_citation(document_number="财税〔2020〕99999号")],
            sections=_answer_sections(),
        )
    )
    assert result.passed is False
    assert any(issue.code == "citation_document_not_found" for issue in result.issues)


def test_citation_verifier_blocks_wrong_article(session) -> None:
    result = CitationVerifier(session).verify(
        VerificationInput(
            citations=[_citation(full_no="第九十九条")], sections=_answer_sections()
        )
    )
    assert result.passed is False
    assert any(issue.code == "citation_article_not_found" for issue in result.issues)


def test_citation_verifier_blocks_content_mismatch(session) -> None:
    """引文与库里那条对不上（张冠李戴）也要拦。"""

    result = CitationVerifier(session).verify(
        VerificationInput(
            citations=[_citation(content="企业发生的合理的工资薪金支出，准予扣除。")],
            sections=_answer_sections(),
        )
    )
    assert result.passed is False
    assert any(issue.code == "citation_content_mismatch" for issue in result.issues)


# ---------------------------------------------------------------------------
# 时效验证器
# ---------------------------------------------------------------------------


def test_temporal_verifier_blocks_repealed(session) -> None:
    result = TemporalVerifier().verify(
        VerificationInput(
            citations=[_citation(effect_status="repealed")], sections=_answer_sections()
        )
    )
    assert result.passed is False
    assert any(issue.code == "citation_not_citable" for issue in result.issues)


def test_temporal_verifier_blocks_superseded(session) -> None:
    result = TemporalVerifier().verify(
        VerificationInput(
            citations=[_citation(effect_status="superseded")], sections=_answer_sections()
        )
    )
    assert result.passed is False


def test_temporal_verifier_blocks_not_yet_effective_without_note(session) -> None:
    """尚未生效的条文，答案里没说明"将施行"就不能引。"""

    blocked = TemporalVerifier().verify(
        VerificationInput(
            citations=[_citation(effect_status="not_yet_effective")], sections=_answer_sections()
        )
    )
    assert blocked.passed is False

    allowed = TemporalVerifier().verify(
        VerificationInput(
            citations=[_citation(effect_status="not_yet_effective")],
            sections=_answer_sections(judgement="该规定将于 2027 年 1 月 1 日起施行。"),
        )
    )
    assert allowed.passed is True


def test_temporal_verifier_blocks_expired_window(session) -> None:
    """执行期已届满的条文不能作为现行依据。"""

    past = datetime.now(timezone.utc) - timedelta(days=1)
    result = TemporalVerifier().verify(
        VerificationInput(
            citations=[_citation(valid_to=int(past.timestamp()))], sections=_answer_sections()
        )
    )
    assert result.passed is False
    assert any(issue.code == "citation_after_expiry" for issue in result.issues)


def test_temporal_verifier_blocks_not_yet_started_window(session) -> None:
    future = datetime.now(timezone.utc) + timedelta(days=30)
    result = TemporalVerifier().verify(
        VerificationInput(
            citations=[_citation(valid_from=int(future.timestamp()))],
            sections=_answer_sections(),
        )
    )
    assert result.passed is False
    assert any(issue.code == "citation_before_effective" for issue in result.issues)


# ---------------------------------------------------------------------------
# 计算复算验证器
# ---------------------------------------------------------------------------


def _calculation(payable: str = "3000.00", surcharge: str = "180.00") -> dict:
    return {
        "vat": {"payable": payable, "steps": [{"title": "应纳税额", "result": payable}]},
        "surcharges": {
            "payable": surcharge,
            "steps": [{"title": "附加税费合计", "result": surcharge}],
        },
    }


def _calc_context() -> dict:
    # 销售额用 30 万（不含税）：小规模纳税人**超过**月销售额 10 万元的免征标准，
    # 因此走"3% 减按 1%"这条路，应纳增值税 = 300,000 × 1% = 3,000.00。
    # 为什么不用 10 万：那个金额现在是免征的（2023 年第 19 号公告），
    # 重算结果是 0.00，测试"复算能发现被篡改的数字"就没有可比的数了。
    return {
        "taxpayer_type": "小规模纳税人",
        "business_type": "销售货物",
        "amount": "300000.00",
        "amount_includes_tax": False,
        "region": "市区",
        # 期间口径也要带上：免税额按月 10 万还是按季 30 万判断，
        # 结论可能不同；复算时漏了它，会把正确答案当成"算错了"。
        "period_scope": "month",
    }


def test_calculation_verifier_accepts_correct_numbers() -> None:
    result = CalculationVerifier().verify(
        VerificationInput(
            sections=_answer_sections(),
            calculation=_calculation(),
            calculation_context=_calc_context(),
        )
    )
    assert result.passed is True
    assert result.checked >= 2


def test_calculation_verifier_flags_tampered_amount() -> None:
    """篡改答案里的数字必须被发现——它是"可修正"级别，不是直接丢弃。"""

    result = CalculationVerifier().verify(
        VerificationInput(
            sections=_answer_sections(),
            calculation=_calculation(payable="9999.00"),
            calculation_context=_calc_context(),
        )
    )
    assert result.passed is True  # 不阻断，因为可以自动修正
    assert result.has_fixable is True
    issue = next(i for i in result.issues if i.code == "calculation_mismatch")
    assert issue.fix == "3000.00"


# ---------------------------------------------------------------------------
# 合规验证器
# ---------------------------------------------------------------------------


def test_compliance_verifier_requires_disclaimer() -> None:
    result = ComplianceVerifier().verify(
        VerificationInput(sections={"judgement": "结论"}, citations=[])
    )
    assert result.passed is False
    assert any(issue.code == "disclaimer_missing" for issue in result.issues)


def test_compliance_verifier_blocks_overreach() -> None:
    result = ComplianceVerifier().verify(
        VerificationInput(
            sections=_answer_sections(judgement="我们可以保证您一定能通过审核。"),
            citations=[],
        )
    )
    assert result.passed is False
    assert any(issue.severity is Severity.BLOCKING for issue in result.issues)


def test_compliance_verifier_passes_normal_answer() -> None:
    result = ComplianceVerifier().verify(
        VerificationInput(sections=_answer_sections(), citations=[])
    )
    assert result.passed is True


# ---------------------------------------------------------------------------
# 失败处理：修正 / 重新生成 / 降级
# ---------------------------------------------------------------------------


def test_fixable_issue_is_corrected_and_output(session) -> None:
    """数字笔误不该让用户等一次重新生成——直接修正后输出。"""

    payload = VerificationInput(
        question="小规模纳税人卖10万货物交多少税",
        citations=[_citation()],
        sections=_answer_sections(calculation=[{"title": "应纳税额", "result": "9999.00"}]),
        calculation=_calculation(payable="9999.00"),
        calculation_context=_calc_context(),
    )
    outcome = AnswerVerifier(session, record=False).verify_and_repair(payload)
    assert outcome.degraded is False
    assert outcome.report.outcome == "fixed"
    assert payload.calculation["vat"]["payable"] == "3000.00"


def test_blocking_issue_degrades_after_retries(session) -> None:
    """无效引用修不回来时必须降级，并说明原因——绝不原样发给用户。"""

    payload = VerificationInput(
        question="测试问题",
        citations=[_citation(document_number="财税〔2020〕99999号")],
        sections=_answer_sections(),
    )
    outcome = AnswerVerifier(session, record=False).verify_and_repair(payload)
    assert outcome.degraded is True
    assert outcome.report.outcome == "degraded"
    assert "没有通过自动验证" in str(outcome.sections["judgement"])
    # 降级输出仍带免责说明
    assert outcome.sections.get("disclaimer")


def test_rebuild_drops_bad_citation_and_passes(session) -> None:
    """重新生成：剔掉问题引用后，用剩下的依据重出一版，能通过就正常输出。"""

    payload = VerificationInput(
        question="测试问题",
        citations=[_citation(), _citation(document_number="财税〔2020〕99999号")],
        sections=_answer_sections(),
    )
    calls: list[int] = []

    def rebuild(inner) -> None:
        calls.append(len(inner.citations))
        inner.sections["judgement"] = "重新生成后的结论"

    outcome = AnswerVerifier(session, record=False).verify_and_repair(payload, rebuild=rebuild)
    assert calls, "重建回调应该被调用"
    assert outcome.degraded is False
    assert len(payload.citations) == 1


def test_verification_is_recorded(session) -> None:
    """每次验证留痕：后台要能统计拦截率与原因分布。"""

    from app.models.verification import VerificationRecord

    payload = VerificationInput(
        question="留痕测试",
        citations=[_citation()],
        sections=_answer_sections(),
    )
    AnswerVerifier(session, record=True).verify_and_repair(payload)
    record = session.query(VerificationRecord).order_by(
        VerificationRecord.created_at.desc()
    ).first()
    assert record is not None
    assert record.outcome == "passed"
    assert record.passed is True

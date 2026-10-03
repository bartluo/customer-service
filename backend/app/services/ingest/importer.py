"""批量导入：把解析好的法规写入数据库。

三条关键设计：
  1. 逐份独立事务。一份失败不影响前面已成功的——任务清单要求能看到失败清单，
     而不是整批回滚。
  2. 幂等。以"文号 + 位阶 + 域"为唯一判据，重复导入跳过而不是产生重复数据。
     采集流水线会反复跑，幂等是硬要求。
  3. 需要人工复核的照常入库，标 pending_review。不因缺文号/缺来源就丢弃，
     因为审核专家需要在复核台上看到它；但它绝不会进入检索结果
     （检索只读 published）。
"""

from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.knowledge.parser import ParsedBlock, ParsedRegulation, parse_regulation
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

DEFAULT_EFFECT_STATUS = "effective"


@dataclass
class DocumentInput:
    """一份待导入的原始文件。"""

    filename: str
    text: str
    source_url: str | None
    retrieved_at: datetime | None = None
    source_file_key: str | None = None
    tax_types: list[str] = field(default_factory=list)
    applies_to: list[str] = field(default_factory=list)
    # 效力状态。采集侧从官方"时效性"字段翻译过来（见 app/domain/effect_status.py）。
    # 为空时按"现行有效"处理——注意这是"不知道"的兜底，不是"已确认有效"。
    effect_status: str | None = None

    def resolved_retrieved_at(self) -> datetime:
        return self.retrieved_at or datetime.now(timezone.utc)


@dataclass
class ImportReport:
    """导入结果汇总：成功数、失败数、待复核数、失败清单、进度。"""

    total: int = 0
    success_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    review_count: int = 0
    article_count: int = 0
    progress_trace: list[int] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    review_items: list[dict[str, str]] = field(default_factory=list)

    @property
    def progress_percent(self) -> float:
        if self.total == 0:
            return 100.0
        return round(len(self.progress_trace) / self.total * 100, 2)

    def to_dict(self) -> dict[str, object]:
        """给脚本与接口用的可序列化汇总。"""

        return {
            "total": self.total,
            "success": self.success_count,
            "skipped": self.skipped_count,
            "failed": self.failed_count,
            "needs_review": self.review_count,
            "articles": self.article_count,
            "progress_percent": self.progress_percent,
            "failures": self.failures,
            "review_items": self.review_items,
        }


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_existing(session: Session, parsed: ParsedRegulation, domain_id: str) -> Regulation | None:
    """按 文号 + 位阶 + 域 找已存在的法规，用于幂等判断。

    没有文号时退回按"标题 + 位阶 + 域"匹配。
    为什么必须退回：文号识别不出来的法规（页面把文号拆成两行、或确实没有文号），
    旧实现直接返回 None，等于"永远认为没导入过"。
    后果是同一份文件每导入一次就在库里多一条，检索结果里同一条法规重复出现。
    采集脚本可以反复跑，幂等是硬要求，所以必须兜住这一条。
    """

    if parsed.document_number:
        statement = select(Regulation).where(
            Regulation.domain_id == domain_id,
            Regulation.document_number == parsed.document_number,
            Regulation.hierarchy_level == parsed.hierarchy_level,
        )
        existing = session.execute(statement).scalars().first()
        if existing is not None:
            return existing

    title = (parsed.title or "").strip()
    if not title:
        return None
    statement = select(Regulation).where(
        Regulation.domain_id == domain_id,
        Regulation.title == title,
        Regulation.hierarchy_level == parsed.hierarchy_level,
    )
    return session.execute(statement).scalars().first()


def _persist_articles(
    session: Session,
    regulation: Regulation,
    blocks: list[ParsedBlock],
    valid_from: datetime,
    source_url: str | None,
    source_file_key: str | None,
    retrieved_at: datetime,
    effect_status: str = DEFAULT_EFFECT_STATUS,
) -> int:
    """写入条文与版本，返回写入的条级条文数。

    只为 article / paragraph / item 三级写版本记录。章、节只是结构目录，
    没有独立可引用的正文内容，不建版本，避免污染检索结果。
    """

    id_by_order: dict[int, str] = {}
    written_articles = 0

    for block in blocks:
        article = RegulationArticle(
            regulation_id=regulation.id,
            level_code=block.level_code,
            article_no=block.article_no,
            full_no=block.full_no,
            heading_path=block.heading_path,
            order_index=block.order_index,
            parent_article_id=(
                id_by_order.get(block.parent_index) if block.parent_index is not None else None
            ),
        )
        session.add(article)
        session.flush()
        id_by_order[block.order_index] = article.id

        if block.level_code not in {"article", "paragraph", "item"}:
            continue

        session.add(
            RegulationArticleVersion(
                article_id=article.id,
                version=1,
                content=block.content,
                valid_from=valid_from,
                valid_to=None,
                effect_status=effect_status,
                source_file_key=source_file_key,
                source_url=source_url,
                retrieved_at=retrieved_at,
                content_hash=_content_hash(block.content),
            )
        )
        if block.level_code == "article":
            written_articles += 1

    return written_articles


def import_one(
    session: Session,
    document: DocumentInput,
    domain_id: str = "finance_tax",
) -> tuple[str, Regulation | None, ParsedRegulation | None, str]:
    """导入一份文件。

    返回 (状态, 法规记录, 解析结果, 说明)。状态取值：
      imported  已导入
      skipped   已存在，跳过（幂等）
      failed    失败，调用方记入失败清单

    本函数自行提交事务；失败时回滚，保证不影响上一份。
    """

    if not document.text or not document.text.strip():
        return "failed", None, None, "文件内容为空"

    parsed = parse_regulation(document.text, source_url=document.source_url)

    existing = _find_existing(session, parsed, domain_id)
    if existing is not None:
        return "skipped", existing, parsed, f"已存在同文号法规：{existing.title}"

    retrieved_at = document.resolved_retrieved_at()
    valid_from = parsed.effective_date or retrieved_at

    try:
        regulation = Regulation(
            domain_id=domain_id,
            title=parsed.title or document.filename,
            document_number=parsed.document_number,
            issuer=parsed.issuer,
            hierarchy_level=parsed.hierarchy_level,
            region_scope="national",
            publish_date=parsed.publish_date,
            effective_date=parsed.effective_date,
            expiry_date=parsed.expiry_date,
            tax_types=document.tax_types,
            applies_to=document.applies_to,
            source_url=document.source_url or "",
            retrieved_at=retrieved_at,
            source_file_key=document.source_file_key,
            content_hash=_content_hash(document.text),
            version=1,
            effect_status=document.effect_status or DEFAULT_EFFECT_STATUS,
            review_state="pending_review" if parsed.requires_review else "published",
            is_draft=False,
        )
        session.add(regulation)
        session.flush()

        article_count = _persist_articles(
            session,
            regulation,
            parsed.blocks,
            valid_from=valid_from,
            source_url=document.source_url,
            source_file_key=document.source_file_key,
            retrieved_at=retrieved_at,
            effect_status=document.effect_status or DEFAULT_EFFECT_STATUS,
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        return "failed", None, parsed, f"数据库约束冲突（可能是时间区间重叠）：{exc.orig}"
    except Exception as exc:  # noqa: BLE001 - 批量导入要兜住所有异常并记入失败清单
        session.rollback()
        return "failed", None, parsed, f"{type(exc).__name__}: {exc}"

    return "imported", regulation, parsed, f"导入成功，写入 {article_count} 条条文"


def import_documents(
    session: Session,
    documents: list[DocumentInput],
    domain_id: str = "finance_tax",
    on_progress=None,
) -> ImportReport:
    """批量导入，逐份独立事务，返回完整报告。"""

    report = ImportReport(total=len(documents))

    for index, document in enumerate(documents, start=1):
        status, regulation, parsed, message = import_one(session, document, domain_id=domain_id)

        if status == "imported":
            report.success_count += 1
            report.article_count += len(parsed.articles)
            if parsed.requires_review:
                report.review_count += 1
                report.review_items.append(
                    {
                        "filename": document.filename,
                        "title": parsed.title,
                        "reasons": "；".join(parsed.review_reasons),
                    }
                )
        elif status == "skipped":
            report.skipped_count += 1
        else:
            report.failed_count += 1
            report.failures.append({"filename": document.filename, "reason": message})

        report.progress_trace.append(index)
        if on_progress is not None:
            on_progress(index, len(documents), document.filename, status)

    return report

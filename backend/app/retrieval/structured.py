"""结构化精确检索。

走 PostgreSQL 而不是向量库。原因：用户问"财税〔2019〕39号第一条"时，
要的是精确命中——语义相似反而是干扰（会召回一堆"看起来像"的条文）。
向量擅长的是"意思相近"，结构化擅长的是"就是这一条"。

硬过滤写在这里而不是调用方，因为财税的红线规则不能依赖每个调用方都记得写：
  · 只返回 published（待复核内容可能还没审过）
  · 只返回 effective / partially_repealed（废止条文绝不能出现在答案里）
  · 时点过滤：valid_from <= as_of < valid_to（左闭右开）
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.knowledge.ontology import CITABLE_EFFECT_STATUSES, HIERARCHY_LEVELS
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

# 引用单位（技术方案 4.1）：条 / 款 / 项
SEARCHABLE_LEVELS = ("article", "paragraph", "item")


@dataclass
class StructuredQuery:
    """结构化检索条件。所有字段都是可选的，空表示不限。"""

    domain_id: str = "finance_tax"
    tax_types: list[str] = field(default_factory=list)
    applies_to: list[str] = field(default_factory=list)
    document_number: str | None = None
    article_no: str | None = None
    hierarchy_levels: list[str] = field(default_factory=list)
    effect_statuses: list[str] = field(default_factory=list)
    region_scopes: list[str] = field(default_factory=list)
    keyword: str | None = None
    as_of: datetime | None = None
    limit: int = 20


@dataclass
class StructuredHit:
    """一条命中结果，带齐引用所需的全部要素。"""

    article_id: str
    article_version_id: str
    regulation_id: str
    content: str
    full_no: str
    article_no: str
    level_code: str
    heading_path: str | None
    document_number: str | None
    regulation_title: str
    issuer: str | None
    hierarchy_level: str
    effect_status: str
    valid_from: datetime
    valid_to: datetime | None
    tax_types: list[str]
    applies_to: list[str]
    source_url: str | None
    regulation_source_url: str | None

    def citation(self) -> str:
        """生成人类可读的引用串，如「财税〔2019〕39号 第一条」。"""

        number = self.document_number or self.regulation_title
        return f"{number} {self.full_no}".strip()


def _jsonb_contains(column, values: list[str]):
    """JSONB 数组包含判断：values 中任一命中即算匹配。

    用 JSONB 的 @> 运算符（包含）而不是 text 模糊匹配：
    精确匹配才能避免"增值税"命中"增值税小规模纳税人优惠"这类误伤。
    """

    from sqlalchemy.dialects.postgresql import JSONB

    return or_(*[column.contains([value]) for value in values])


def search_structured(db: Session, query: StructuredQuery) -> list[StructuredHit]:
    """执行结构化检索，返回命中列表。"""

    statement = (
        select(RegulationArticle, RegulationArticleVersion, Regulation)
        .join(RegulationArticleVersion, RegulationArticleVersion.article_id == RegulationArticle.id)
        .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
        .where(
            Regulation.domain_id == query.domain_id,
            # 硬过滤：只查可引用层级
            RegulationArticle.level_code.in_(SEARCHABLE_LEVELS),
            # 硬过滤：只查已发布。未审内容不能被引用
            Regulation.review_state == "published",
            # 硬过滤：只查可引用状态（废止 / 已被替代 / 草案一律排除）
            RegulationArticleVersion.effect_status.in_(
                query.effect_statuses or CITABLE_EFFECT_STATUSES
            ),
        )
    )

    if query.tax_types:
        statement = statement.where(_jsonb_contains(Regulation.tax_types, query.tax_types))
    if query.applies_to:
        statement = statement.where(_jsonb_contains(Regulation.applies_to, query.applies_to))
    if query.document_number:
        statement = statement.where(Regulation.document_number == query.document_number)
    if query.hierarchy_levels:
        statement = statement.where(Regulation.hierarchy_level.in_(query.hierarchy_levels))
    if query.region_scopes:
        statement = statement.where(Regulation.region_scope.in_(query.region_scopes))
    if query.article_no:
        # 条号既可能精确等于"第一条"，也可能出现在 full_no 里
        statement = statement.where(
            or_(
                RegulationArticle.article_no == query.article_no,
                RegulationArticle.full_no.contains(query.article_no),
            )
        )
    if query.keyword:
        statement = statement.where(
            or_(
                RegulationArticleVersion.content.contains(query.keyword),
                Regulation.title.contains(query.keyword),
            )
        )
    if query.as_of:
        # 左闭右开：[valid_from, valid_to)
        statement = statement.where(RegulationArticleVersion.valid_from <= query.as_of)
        statement = statement.where(
            or_(
                RegulationArticleVersion.valid_to.is_(None),
                RegulationArticleVersion.valid_to > query.as_of,
            )
        )

    statement = statement.order_by(
        RegulationArticleVersion.effect_status.desc(),
        Regulation.hierarchy_level.asc(),
        Regulation.publish_date.desc(),
    ).limit(query.limit)

    results: list[StructuredHit] = []
    for article, version, regulation in db.execute(statement).all():
        results.append(
            StructuredHit(
                article_id=article.id,
                article_version_id=version.id,
                regulation_id=regulation.id,
                content=version.content,
                full_no=article.full_no,
                article_no=article.article_no,
                level_code=article.level_code,
                heading_path=article.heading_path,
                document_number=regulation.document_number,
                regulation_title=regulation.title,
                issuer=regulation.issuer,
                hierarchy_level=regulation.hierarchy_level,
                effect_status=version.effect_status,
                valid_from=version.valid_from,
                valid_to=version.valid_to,
                tax_types=list(regulation.tax_types or []),
                applies_to=list(regulation.applies_to or []),
                source_url=version.source_url,
                regulation_source_url=regulation.source_url,
            )
        )
    return results

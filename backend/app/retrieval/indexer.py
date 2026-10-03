"""把 PostgreSQL 的条文灌进 Qdrant（写入侧）。

数据流：
  regulation_articles + regulation_article_versions
    → 组装成可检索的条文条目（带 payload）
    → 调 embedding 服务取稠密 + 稀疏向量
    → 写入 kb_articles

三个设计决定：
  1. 只索引"可引用层级"（条 / 款 / 项）。章、节只是目录，
     索引它们会稀释检索结果。
  2. 只索引 published 的法规。待复核法规一旦被检索到就是事故——
     内容可能还没审过。做法是过滤 review_state，而不是事后清理。
  3. payload 里带 valid_from_ts / valid_to_ts（Unix 秒），
     因为 Qdrant 的范围过滤只认数值，日期字符串没法做区间查询。
"""

from __future__ import annotations
import hashlib
import logging
from datetime import datetime, timezone

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, SparseVector
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.retrieval.collections import KB_ARTICLES
from app.retrieval.embedding_client import get_embedding_client

logger = logging.getLogger(__name__)

# 只有这三层是最小可引用单位（技术方案 4.1）
INDEXABLE_LEVELS = ("article", "paragraph", "item")
BATCH_SIZE = 64


def _stable_point_id(article_version_id: str) -> str:
    """用条文版本 ID 派生稳定的 point ID。

    为什么不用随机 UUID：条文版本 ID 是数据库主键，重建索引时
    用它派生同样的 point ID，upsert 就是覆盖而不是产生重复点。
    Qdrant 的 point ID 必须是 UUID 或无符号整数，这里用 MD5 转 UUID。
    """

    digest = hashlib.md5(article_version_id.encode("utf-8")).hexdigest()
    return f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _to_timestamp(value: datetime | None) -> int | None:
    """转 Unix 秒。Qdrant 的整数索引范围过滤只能处理数值。"""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def build_index_entries(db: Session, domain_id: str = "finance_tax") -> list[dict]:
    """从数据库读出可索引的条文条目。

    返回 [{point_id, dense_text, payload}]。此时还没有向量，
    向量化在写入前批量做——embedding 服务按批处理效率高得多。
    """

    statement = (
        select(RegulationArticle, RegulationArticleVersion, Regulation)
        .join(RegulationArticleVersion, RegulationArticleVersion.article_id == RegulationArticle.id)
        .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
        .where(
            Regulation.domain_id == domain_id,
            # 只索引可引用层级：章、节不建向量
            RegulationArticle.level_code.in_(INDEXABLE_LEVELS),
            # 只索引已发布的：待复核内容可能还没审过，不能被检索到
            Regulation.review_state == "published",
            # 只索引仍有效的版本区间（有结束日的版本已失效，不该被召回）
            RegulationArticleVersion.effect_status.in_(("effective", "partially_repealed")),
        )
        .options(selectinload(RegulationArticle.versions))
    )

    return _collect_entries(db, statement, domain_id)


def _collect_entries(db: Session, statement, domain_id: str) -> list[dict]:
    """执行查询并把行组装成索引条目（跳过空正文）。"""

    entries: list[dict] = []
    for article, version, regulation in db.execute(statement).all():
        entry = _build_entry(article, version, regulation, domain_id)
        if entry is not None:
            entries.append(entry)
    return entries


def _build_entry(
    article: RegulationArticle,
    version: RegulationArticleVersion,
    regulation: Regulation,
    domain_id: str,
) -> dict | None:
    """组装单条索引条目（检索文本 + payload）。正文为空时返回 None。"""

    # 检索文本 = 法规标题 + 条文正文。
    #
    # 为什么必须带标题：用户是按"哪份文件"来问的，不是说条文原话。
    # 问"全面数字化电子发票怎么开"，能对上的是
    # 《国家税务总局关于推广应用全面数字化电子发票的公告》，
    # 而它的条文正文里根本没出现"全面数字化"这四个字——
    # 只把正文放进向量，这个问题就永远匹配不到那份公告。
    #
    # 早期版本担心"标题每条都重复会拉低区分度"，这个顾虑是真实但很小的：
    # 标题只有十几个字，条文正文动辄几十上百字，占比很低；
    # 而少了标题，整类"按文件问"的问题全部失效，代价大得多。
    # 靠"标题命中"还能被稀疏路由精确匹配到，两边都受益。
    content = (version.content or "").strip()
    # 正文为空的条目不索引。判断要用正文而不是拼好的文本——
    # 拼上标题以后 dense_text 永远非空，拿它当判断条件等于没判断。
    if not content:
        return None

    dense_text = f"{regulation.title}\n{content}"
    payload = {
        "domain_id": domain_id,
        "regulation_id": regulation.id,
        "article_id": article.id,
        "article_version_id": version.id,
        "document_number": regulation.document_number,
        "regulation_title": regulation.title,
        "issuer": regulation.issuer,
        "hierarchy_level": regulation.hierarchy_level,
        "region": regulation.region_scope,
        "level_code": article.level_code,
        "full_no": article.full_no,
        "heading_path": article.heading_path,
        "effect_status": version.effect_status,
        "review_state": regulation.review_state,
        "valid_from_ts": _to_timestamp(version.valid_from),
        "valid_to_ts": _to_timestamp(version.valid_to),
        "tax_types": regulation.tax_types or [],
        "content": version.content,
        "source_url": version.source_url,
    }
    return {
        "point_id": _stable_point_id(version.id),
        "dense_text": dense_text,
        "payload": payload,
    }


def build_regulation_entries(
    db: Session, regulation_id: str, domain_id: str = "finance_tax"
) -> list[dict]:
    """只取某一份法规的可索引条目。口径与全量构建完全一致。"""

    statement = (
        select(RegulationArticle, RegulationArticleVersion, Regulation)
        .join(RegulationArticleVersion, RegulationArticleVersion.article_id == RegulationArticle.id)
        .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
        .where(
            Regulation.id == regulation_id,
            Regulation.domain_id == domain_id,
            RegulationArticle.level_code.in_(INDEXABLE_LEVELS),
            Regulation.review_state == "published",
            RegulationArticleVersion.effect_status.in_(("effective", "partially_repealed")),
        )
    )
    return _collect_entries(db, statement, domain_id)


def _regulation_selector(regulation_id: str):
    """按 regulation_id 定位索引点的删除条件。"""

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    return Filter(
        must=[FieldCondition(key="regulation_id", match=MatchValue(value=regulation_id))]
    )


def remove_regulation_points(
    regulation_id: str, client: QdrantClient | None = None
) -> None:
    """把某份法规的所有索引点删掉（撤回 / 驳回 / 下架用）。"""

    from app.retrieval.qdrant_client import get_client

    client = client or get_client()
    client.delete(
        collection_name=KB_ARTICLES,
        points_selector=_regulation_selector(regulation_id),
        wait=True,
    )


def reindex_regulation(
    db: Session,
    regulation_id: str,
    client: QdrantClient | None = None,
    domain_id: str = "finance_tax",
) -> dict:
    """重建单份法规的索引点：先删旧点，再按当前状态写入。

    为什么需要"单份重建"而不是每次全量重建：
      全量重建在 CPU 上要十几分钟，而"审核专家发布/驳回一份法规"
      "某份法规被标注为废止"这类事件每天都在发生。全量重建的后果是
      索引长期落后于数据库——**数据库里已经废止的条文还能被检索到**，
      这正是财税场景最不能接受的一类错误。

    先删后写，语义就是"这份法规在索引里的样子 = 它此刻在数据库里的样子"：
      · 被驳回 / 被整体废止 → 删完没有可写条目，等于从索引里下架
      · 复审通过 / 时效恢复 → 删完按新状态写回，立即能被检索到
      条目数变少时（例如某一条被单独废止），只 upsert 会把旧点留在索引里，
      所以删除这一步不能省。
    """

    from qdrant_client.models import PointStruct, SparseVector

    from app.retrieval.collections import ensure_collections
    from app.retrieval.qdrant_client import get_client

    client = client or get_client()
    ensure_collections(client)

    entries = build_regulation_entries(db, regulation_id, domain_id=domain_id)
    if not entries:
        # 没有可索引条目 = 这份法规此刻不该出现在检索结果里，删点是正确动作，
        # 不是"没数据所以跳过"。
        remove_regulation_points(regulation_id, client=client)
        return {"regulation_id": regulation_id, "indexed": 0, "removed_only": True}

    embedder = get_embedding_client()
    vectors = embedder.embed([item["dense_text"] for item in entries])
    if vectors is None:
        # 向量服务不可用时保留索引原样，不把这份法规删成"查不到"。
        logger.error("embedding 服务不可用，单份重建跳过（regulation_id=%s）", regulation_id)
        return {"regulation_id": regulation_id, "indexed": 0, "aborted": "embedding_unavailable"}

    points = []
    for offset, item in enumerate(entries):
        sparse_raw = vectors["sparse"][offset]
        points.append(
            PointStruct(
                id=item["point_id"],
                vector={
                    "dense": vectors["dense"][offset],
                    "sparse": SparseVector(
                        indices=sparse_raw["indices"], values=sparse_raw["values"]
                    ),
                },
                payload=item["payload"],
            )
        )

    remove_regulation_points(regulation_id, client=client)
    client.upsert(collection_name=KB_ARTICLES, points=points, wait=True)
    logger.info("单份法规索引重建完成：regulation_id=%s，写入 %d 条", regulation_id, len(points))
    return {"regulation_id": regulation_id, "indexed": len(points)}


def index_articles(
    db: Session,
    client: QdrantClient | None = None,
    domain_id: str = "finance_tax",
    batch_size: int = BATCH_SIZE,
) -> dict:
    """把条文全量灌进 kb_articles。返回统计信息。

    先 delete 掉该域的旧点再重建，保证"删掉的条文不会残留在索引里"。
    这是财税场景的硬要求：法规被删或被驳回后，检索必须立刻查不到。
    """

    from app.retrieval.collections import ensure_collections
    from app.retrieval.qdrant_client import get_client

    client = client or get_client()
    ensure_collections(client)

    # 先确认向量服务可用，再动旧数据。
    # 为什么顺序这么重要：如果先清空再发现向量服务不可用，索引就变成空的——
    # "索引是旧的"只是不新鲜（还能查、只是少了新法规），
    # "索引是空的"是全部查不到。宁可保留旧索引也不能清空。
    embedder = get_embedding_client()
    if embedder.embed(["索引重建前置检查"]) is None:
        logger.error(
            "embedding 服务不可用（%s），本次不重建索引，保留原有索引点", embedder.base_url
        )
        return {
            "total": 0,
            "indexed": 0,
            "skipped_no_vector": 0,
            "failed_batches": 0,
            "aborted": "embedding_unavailable",
        }

    # 清理该域旧点：只删本域，保留其他域的数据
    # 这一步必须在"没有可索引条目"判断之前做。全部法规都被驳回时，
    # 旧点必须被清空——否则"紧急下架"就失效了，而那正是最需要它的场景。
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client.delete(
        collection_name=KB_ARTICLES,
        points_selector=Filter(
            must=[FieldCondition(key="domain_id", match=MatchValue(value=domain_id))]
        ),
        wait=True,
    )

    entries = build_index_entries(db, domain_id=domain_id)
    total = len(entries)
    if total == 0:
        logger.warning("没有可索引的条文（域=%s），已清空该域索引点", domain_id)
        return {"total": 0, "indexed": 0, "skipped_no_vector": 0, "failed_batches": 0}

    # 按文本长度排序再分批。为什么要这么做：
    #   模型推理时一个批次会被补齐到该批最长的那条文本的长度，
    #   所以"63 条短条文 + 1 条长条文"的代价等于 64 条长条文。
    #   实测（CPU 跑 bge-m3）：64 条 30 字的条文 5.9 秒；
    #   把其中一条换成 600 字，同一批变成 79 秒——13 倍。
    #   按长度排序后，每批内部长度接近，补齐浪费几乎消失。
    #   排序不影响结果：point_id 由条文版本 ID 派生，与顺序无关。
    entries = sorted(entries, key=lambda item: len(item["dense_text"]))

    indexed = 0
    skipped_no_vector = 0
    failed_batches = 0

    for start in range(0, total, batch_size):
        chunk = entries[start : start + batch_size]
        vectors = embedder.embed([item["dense_text"] for item in chunk])

        if vectors is None:
            # embedding 服务不可用：稠密与稀疏都建不了，跳过这批
            skipped_no_vector += len(chunk)
            continue

        points = []
        for offset, item in enumerate(chunk):
            sparse_raw = vectors["sparse"][offset]
            points.append(
                PointStruct(
                    id=item["point_id"],
                    vector={
                        "dense": vectors["dense"][offset],
                        "sparse": SparseVector(
                            indices=sparse_raw["indices"],
                            values=sparse_raw["values"],
                        ),
                    },
                    payload=item["payload"],
                )
            )

        try:
            client.upsert(collection_name=KB_ARTICLES, points=points, wait=True)
            indexed += len(points)
        except Exception as exc:  # noqa: BLE001 - 单批失败不应中断整轮索引
            failed_batches += 1
            logger.error("批次 %d-%d 写入失败：%s", start, start + len(chunk), exc)

    logger.info(
        "索引完成：共 %d 条，成功 %d 条，无向量跳过 %d 条，失败批次 %d",
        total, indexed, skipped_no_vector, failed_batches,
    )
    return {
        "total": total,
        "indexed": indexed,
        "skipped_no_vector": skipped_no_vector,
        "failed_batches": failed_batches,
    }

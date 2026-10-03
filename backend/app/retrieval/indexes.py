"""payload 索引的补建与检查。

为什么独立成文件：集合创建是一次性的，索引可能随字段演进而新增。
把补索引从ensure_collections 里拆出来，启动路径就不会被索引任务拖慢。
"""

from __future__ import annotations
import logging

from qdrant_client import QdrantClient
from qdrant_client.models import PayloadSchemaType

from app.retrieval.collections import COLLECTIONS, PAYLOAD_INDEXES

logger = logging.getLogger(__name__)


def rebuild_indexes(client: QdrantClient) -> dict[str, list[str]]:
    """为所有集合补齐 payload 索引，返回 {集合名: 成功创建的字段}。"""

    result: dict[str, list[str]] = {}
    for name in COLLECTIONS:
        created: list[str] = []
        for field, schema in PAYLOAD_INDEXES.get(name, {}).items():
            try:
                client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=schema,
                )
                created.append(field)
            except Exception:  # noqa: BLE001 - 已存在是正常情况
                logger.debug("索引 %s.%s 已存在", name, field)
        result[name] = created
        if created:
            logger.info("集合 %s 补建索引：%s", name, ", ".join(created))
    return result


def missing_indexes(client: QdrantClient) -> dict[str, list[str]]:
    """检查哪些 payload 索引还没建，供健康检查用。"""

    missing: dict[str, list[str]] = {}
    for name in COLLECTIONS:
        try:
            info = client.get_collection(name)
        except Exception:  # noqa: BLE001
            missing[name] = list(PAYLOAD_INDEXES.get(name, {}).keys())
            continue
        schema = getattr(info, "payload_schema", None) or getattr(
            info.config, "payload_schema", {}
        )
        absent = [f for f in PAYLOAD_INDEXES.get(name, {}) if f not in schema]
        if absent:
            missing[name] = absent
    return missing

"""Qdrant 连接管理。

为什么用模块级单例：Qdrant 客户端内部维护 HTTP 连接池，
每次检索都新建会浪费握手开销。进程内复用同一个客户端。
"""

from __future__ import annotations
from functools import lru_cache

from qdrant_client import QdrantClient

from app.config import get_settings


@lru_cache
def get_client() -> QdrantClient:
    """返回进程内复用的 Qdrant 客户端。

    prefer_grpc 不设，即用 HTTP。当前数据量（十万级）下 HTTP 延迟完全够用，
    且容器网络下 HTTP 比 gRPC 省事。
    """

    settings = get_settings()
    return QdrantClient(url=settings.qdrant_url, timeout=10)


def reset_client() -> None:
    """清掉缓存的客户端。

    测试里改了 QDRANT_URL 之后必须调用，否则 lru_cache 会继续用旧地址。
    """

    get_client.cache_clear()

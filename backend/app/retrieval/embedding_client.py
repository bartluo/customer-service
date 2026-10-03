"""调用 embedding 服务取稠密 + 稀疏向量。

为什么不在后端直接跑模型：模型推理吃内存（bge-m3 约 2GB），
独立成服务后可以独立扩容，也便于开发期用 stub 后端秒级联调。

降级策略：embedding 服务不可用时不能让整个检索挂掉——
此时稀疏检索（纯词匹配，走 PostgreSQL 全文）仍能工作，
所以这里返回 None 由调用方决定降级路径，而不是抛异常。
"""

from __future__ import annotations
import logging
import os

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# 超时给得比较宽：真实 bge-m3 在 CPU 上跑一批 64 条条文要十几秒到一分钟，
# 慢的时候更久。旧值 30 秒会把"算得慢"误判成"服务挂了"，
# 结果是这批条文被静默跳过——索引看着建完了，其实缺了一块。
DEFAULT_TIMEOUT_SECONDS = 300.0


class EmbeddingClient:
    """embedding 服务的 HTTP 客户端。"""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.embedding_url).rstrip("/")
        if timeout is None:
            timeout = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
        self.timeout = timeout

    def embed(self, texts: list[str]) -> dict | None:
        """返回 {"dense": [[...]], "sparse": [{"indices":[], "values":[]}]}。

        失败返回 None——调用方负责降级，不在这里抛异常。
        原因：向量服务是检索的增强项而非必需项，
        把它做成硬依赖会让一次短暂的网络抖动变成整站不可用。
        """

        if not texts:
            return {"dense": [], "sparse": []}
        try:
            response = httpx.post(
                f"{self.base_url}/embed",
                json={"texts": texts},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - 检索层需要优雅降级
            logger.warning(
                "embedding 服务调用失败（%d 条文本，超时 %.0f 秒）：%s。"
                "这一批不会进索引——如果反复出现，先确认模型服务是否在跑，"
                "CPU 推理慢时用 EMBEDDING_TIMEOUT_SECONDS 放大超时。",
                len(texts),
                self.timeout,
                exc,
            )
            return None

    def health(self) -> dict:
        """探活，供健康检查使用。"""

        try:
            response = httpx.get(f"{self.base_url}/health", timeout=5.0)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}


_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client

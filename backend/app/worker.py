"""Celery 任务队列。

目前两个任务：
  · ping           —— 连通性验证
  · rebuild_index  —— 复核放行后重建向量索引（见下）
"""

import logging

from celery import Celery

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

celery_app = Celery(
    "customer_service",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    worker_hijack_root_logger=False,
)


@celery_app.task(name="app.ping")
def ping() -> str:
    """验证任务队列连通性。"""

    return "pong"


@celery_app.task(name="app.rebuild_index")
def rebuild_index(domain_id: str = "finance_tax") -> dict:
    """重建某个域的向量索引。

    为什么改成异步任务而不是让接口同步等：
      条文向量化要调 embedding 服务逐批算，几千条条文要几十秒到几分钟。
      审核专家在复核台点"放行"时不能卡这么久，所以放行接口只负责改状态，
      索引更新丢给任务队列，失败也不影响"已经放行"这个事实（可重复执行，幂等）。
    """

    from app.database.session import SessionLocal
    from app.retrieval.indexer import index_articles
    from app.retrieval.qdrant_client import get_client

    session = SessionLocal()
    try:
        stats = index_articles(session, client=get_client(), domain_id=domain_id)
    finally:
        session.close()

    logger.info("向量索引重建完成 domain=%s stats=%s", domain_id, stats)
    return stats

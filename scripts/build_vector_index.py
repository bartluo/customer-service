"""把数据库里的法规条文全量灌进向量索引（Qdrant）。

用法（项目根目录）：
    python scripts/build_vector_index.py
    python scripts/build_vector_index.py --domain finance_tax

什么时候必须跑这个脚本：
  · 导入新法规之后
  · 审核专家发布 / 驳回法规之后（发布状态的改变必须同步到索引）
  · 法规被撤回或删除之后（否则已撤回的条文仍会被检索到）

这个脚本是"重建"不是"追加"：它先清空该域的旧点再全量写入。
财税场景要求"库里没有的，答案里也必须没有"，所以宁可全量重建，
不做增量合并——增量合并会留下已删除条文的残留点。
"""

from __future__ import annotations
import argparse
import os
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))


def _load_env() -> None:
    """加载 .env 并把容器主机名换成本机地址。

    .env 里写的是容器内主机名（postgres / qdrant …），
    本机直接跑脚本时解析不了，要换成 127.0.0.1（Docker 端口已映射到本机）。
    """

    env_file = REPO / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

    db_url = os.environ.get("DATABASE_URL", "")
    for container_host in ("@postgres:", "@qdrant:", "@redis:", "@embedding:"):
        if container_host in db_url:
            os.environ["DATABASE_URL"] = db_url.replace(container_host, "@127.0.0.1:")
            break

    # QDRANT_URL / EMBEDDING_URL / REDIS_URL 是纯主机名形式（没有 "@"），
    # 上面那个循环处理不到，这里按 "协议://主机名" 拆开把主机名换成本机地址。
    for key, container_name in (
        ("QDRANT_URL", "qdrant"),
        ("EMBEDDING_URL", "embedding"),
        ("REDIS_URL", "redis"),
    ):
        value = os.environ.get(key, "")
        if f"//{container_name}:" not in value:
            continue
        scheme, _, rest = value.partition("://")
        _, _, port_and_path = rest.partition(":")
        os.environ[key] = f"{scheme}://127.0.0.1:{port_and_path}"


def main() -> int:
    parser = argparse.ArgumentParser(description="重建财税法规向量索引")
    parser.add_argument("--domain", default="finance_tax", help="域包标识，默认 finance_tax")
    args = parser.parse_args()

    _load_env()

    from app.database.session import SessionLocal
    from app.retrieval.indexer import index_articles
    from app.retrieval.qdrant_client import get_client

    db = SessionLocal()
    try:
        stats = index_articles(db, client=get_client(), domain_id=args.domain)
    finally:
        db.close()

    print("向量索引重建完成")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    if stats["skipped_no_vector"] > 0:
        print(
            f"警告：有 {stats['skipped_no_vector']} 条条文没能拿到向量（embedding 服务超时或不可用），"
            "这些条文检索不到。请确认模型服务正常后重跑本脚本。"
        )
        return 1
    if stats["failed_batches"] > 0:
        print(f"警告：有 {stats['failed_batches']} 个批次写入向量库失败，请重跑本脚本。")
        return 1
    if stats["indexed"] == 0 and stats["total"] > 0:
        print("警告：有可索引条文但一条都没写进去，通常是 embedding 服务不可用")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

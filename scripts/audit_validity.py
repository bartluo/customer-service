"""时效性核查：把官方"时效性"落到库里的效力状态，并清理已废止内容的索引点（D 专题）。

用法（项目根目录）：
    python scripts/audit_validity.py                       # 只看差异
    python scripts/audit_validity.py --apply               # 真的改
    python scripts/audit_validity.py --apply --rebuild     # 改完顺带全量重建索引

为什么需要它：
  法规库里的内容不是"导入即有效"。官方政策法规库对每份文件都标了时效性
  （全文有效 / 已修改 / 全文废止 / 全文失效 / 尚未生效），
  采集时把它们统一按"现行有效"入库了，后果是**已废止的法规照样被引出来**——
  问增值税，答案里可能引用《增值税暂行条例》（2026 年已废止）。

  这不是数据多少的问题，是答案对错的问题。

本脚本做三件事：
  1. 读采集时留下的 sidecar 元数据，把官方时效性翻译成本项目效力状态；
  2. 更新数据库（法规主表 + 条文版本表，两处都要改——检索过滤看的是版本表）；
  3. 同步向量库 payload，并把不可引用的点从索引里删掉。

为什么用删点而不是全量重建：重建要 20 多分钟且要重算全部向量，
而这一步只是"把不该出现的条目移除"，删点几秒钟就完成。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


def _normalize(title: str) -> str:
    """标题归一化，用于跨来源匹配（采集侧标题与入库标题可能有细微标点差异）。"""

    return re.sub(r"[\s（）()《》〔〕\[\]—\-－_、，,。.]+", "", title or "")


_BLANK_VALUES = {"", "null", "none", "undefined", "nan"}


def _clean_date(value: object) -> str | None:
    """清洗日期字段。

    官方接口对空字段返回的是字符串 "null"，早期采集把它们原样写进了元数据；
    直接拿去写库会报 invalid input syntax for type timestamp。
    """

    text = str(value or "").strip()
    if text.lower() in _BLANK_VALUES:
        return None
    # 只接受形如 2026-09-30 的日期
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def load_official_status() -> dict[str, dict]:
    """从采集留下的 sidecar 里汇总官方时效性：归一化标题 → {aging, abolish_date}。"""

    result: dict[str, dict] = {}
    for directory in REPO.glob("法规数据/待导入*"):
        for meta_file in directory.glob("*.meta.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            title = meta.get("title") or ""
            if not title:
                continue
            aging = (meta.get("effect_status_hint") or "").strip()
            if not aging:
                continue
            result[_normalize(title)] = {
                "aging": aging,
                "abolish_date": _clean_date(meta.get("expiry_date")),
            }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="时效性核查与修复")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只打印差异）")
    parser.add_argument("--rebuild", action="store_true", help="改完后全量重建索引")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from sqlalchemy import select

    from app.database.session import SessionLocal
    from app.domain.effect_status import from_official_aging
    from app.knowledge.ontology import CITABLE_EFFECT_STATUSES
    from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

    official = load_official_status()
    print(f"官方时效性记录：{len(official)} 条")

    session = SessionLocal()
    changes: list[tuple[str, str, str, str]] = []  # (regulation_id, title, old, new)
    unknown = 0
    try:
        regulations = session.execute(select(Regulation)).scalars().all()
        for regulation in regulations:
            meta = official.get(_normalize(regulation.title))
            if not meta:
                unknown += 1
                continue
            target = from_official_aging(meta["aging"])
            if not target or target == regulation.effect_status:
                continue
            changes.append((regulation.id, regulation.title, regulation.effect_status, target))

        print(f"库里法规 {len(regulations)} 份，其中 {unknown} 份没有官方时效性记录")
        print(f"需要修正：{len(changes)} 份\n")

        # 按目标状态分组展示，便于人工核对
        grouped: dict[str, list[str]] = {}
        for _id, title, _old, new in changes:
            grouped.setdefault(new, []).append(title)
        for status, titles in sorted(grouped.items(), key=lambda item: -len(item[1])):
            print(f"  → {status}（{len(titles)} 份）")
            for title in titles[:8]:
                print(f"      {title[:56]}")
            if len(titles) > 8:
                print(f"      …… 其余 {len(titles) - 8} 份")

        if not args.apply:
            print("\n这是预览。加 --apply 才会真正写库。")
            return 0

        if changes:
            # 1) 改数据库：主表 + 条文版本表都要改（检索过滤看的是版本表）
            for regulation_id, _title, _old, new in changes:
                regulation = session.get(Regulation, regulation_id)
                if regulation is None:
                    continue
                regulation.effect_status = new
                meta = official.get(_normalize(regulation.title), {})
                if meta.get("abolish_date") and not regulation.expiry_date:
                    regulation.expiry_date = meta["abolish_date"]

                versions = session.execute(
                    select(RegulationArticleVersion)
                    .join(
                        # 版本的 article_id 指向条文，经条文回到法规
                        RegulationArticle,
                        RegulationArticle.id == RegulationArticleVersion.article_id,
                    )
                    .where(RegulationArticle.regulation_id == regulation_id)
                ).scalars().all()
                for version in versions:
                    version.effect_status = new

            from app.services import audit

            audit.record(
                session,
                action="knowledge.effect_status_audited",
                actor_id=None,
                actor_username="system:audit_validity",
                target_type="regulation_batch",
                detail={"changed": len(changes)},
            )
            session.commit()
            print(f"\n数据库已更新：{len(changes)} 份法规及其全部条文版本")
        else:
            print("\n数据库没有需要修正的，继续做索引清理。")

        # 2) 同步向量库：改 payload，再把不可引用的点删掉
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        from app.retrieval.collections import KB_ARTICLES
        from app.retrieval.qdrant_client import get_client

        client = get_client()
        for regulation_id, _title, _old, new in changes:
            client.set_payload(
                collection_name=KB_ARTICLES,
                payload={"effect_status": new},
                points=Filter(
                    must=[
                        FieldCondition(key="regulation_id", match=MatchValue(value=regulation_id))
                    ]
                ),
                wait=True,
            )
        if changes:
            print(f"向量库 payload 已同步：{len(changes)} 份法规")

        # 清掉索引里所有"不可引用"的点。
        # 用 must_not 白名单而不是列举黑名单：黑名单会漏（容易漏掉
        # not_yet_effective 这一类），白名单天然与检索过滤口径一致——
        # 检索只认 CITABLE_EFFECT_STATUSES，索引里就不该留别的东西。
        removed = client.delete(
            collection_name=KB_ARTICLES,
            points_selector=Filter(
                must_not=[
                    FieldCondition(
                        key="effect_status",
                        match=MatchAny(any=list(CITABLE_EFFECT_STATUSES)),
                    )
                ]
            ),
            wait=True,
        )
        print(f"已从索引移除不可引用条文（{removed.status if removed else 'ok'}）")

        if args.rebuild:
            from app.retrieval.indexer import index_articles

            stats = index_articles(session, client=client, domain_id="finance_tax")
            print("向量索引已重建：")
            for key, value in stats.items():
                print(f"  {key}: {value}")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

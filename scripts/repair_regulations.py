"""按源文件重新解析，修复库里与源文件不一致的法规元数据。

用法（项目根目录）：
    python scripts/repair_regulations.py --inbox "法规数据\待导入"          # 只看差异
    python scripts/repair_regulations.py --inbox "法规数据\待导入" --apply  # 真的改

为什么需要它：
  解析器（parser.py）修好某个字段的识别规则后，已经入库的数据不会自动变好——
  它们带着旧规则下的错误元数据留在库里，而且因为"文号相同就算重复"，
  重新导入会被跳过，改不动。

  所以修复路径是：拿源文件重新解析一遍 → 跟库里的记录逐字段比对 → 有差异才更新。
  这样既不会重复插入，也不会漏掉修复。

匹配方式用 content_hash（导入时对文件全文算的 SHA256），而不是文号或标题，
因为出问题的恰恰就是标题和文号。

安全：默认只打印差异不写库；加 --apply 才真正更新，并写审计日志。
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

# 会被比对的字段。只比对"能从源文件重新解析出来"的那些，
# 人工在复核环节补的字段（比如 applies_to）不动。
REPAIRABLE_FIELDS = (
    "title",
    "document_number",
    "issuer",
    "hierarchy_level",
    "publish_date",
    "effective_date",
    "expiry_date",
)

# 单独处理：税种不在解析结果里，而是采集侧推断出来的（DocumentInput.tax_types）。
# 它同时存在于数据库和向量库 payload（检索按税种过滤要用），两处都要同步。
TAX_TYPES_FIELD = "tax_types"


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="按源文件修复法规元数据")
    parser.add_argument("--inbox", required=True, help="法规文件目录")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只打印差异）")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from sqlalchemy import select

    from app.database.session import SessionLocal
    from app.knowledge.parser import parse_regulation
    from app.models.knowledge import Regulation
    from app.services.ingest.collector import collect_from_directory

    inbox = pathlib.Path(args.inbox).resolve()
    documents = collect_from_directory(inbox)
    if not documents:
        print(f"[提示] {inbox} 里没有可解析的文件")
        return 0

    session = SessionLocal()
    changed = 0
    missing = 0
    tax_type_changes: list[tuple[str, list, list]] = []
    try:
        for document in documents:
            digest = _content_hash(document.text)
            record = session.execute(
                select(Regulation).where(Regulation.content_hash == digest)
            ).scalars().first()
            if record is None:
                missing += 1
                print(f"  [未入库] {document.filename}")
                continue

            parsed = parse_regulation(document.text, source_url=document.source_url)
            diffs: list[tuple[str, object, object]] = []
            for field_name in REPAIRABLE_FIELDS:
                new_value = getattr(parsed, field_name)
                old_value = getattr(record, field_name)
                if new_value != old_value:
                    diffs.append((field_name, old_value, new_value))

            # 税种：来自采集侧推断，不在解析结果里，单独比对
            old_tax_types = list(record.tax_types or [])
            new_tax_types = list(document.tax_types or [])
            if sorted(old_tax_types) != sorted(new_tax_types):
                diffs.append((TAX_TYPES_FIELD, old_tax_types, new_tax_types))
                tax_type_changes.append((record.id, old_tax_types, new_tax_types))

            if not diffs:
                continue

            changed += 1
            print(f"\n  [需修复] {document.filename}")
            for field_name, old_value, new_value in diffs:
                print(f"      {field_name}: {old_value!r} → {new_value!r}")
                if args.apply:
                    setattr(record, field_name, new_value)

        if args.apply and changed:
            from app.services import audit

            audit.record(
                session,
                action="knowledge.regulations_repaired",
                actor_id=None,
                actor_username="system:repair_regulations",
                target_type="regulation_batch",
                detail={"changed": changed, "inbox": str(inbox)},
            )
            session.commit()

        # 向量库 payload 里的税种也要同步：检索按税种过滤走的是 payload，
        # 只改数据库不改 payload 的话，"过滤"仍然按旧值排除法规。
        # 这里只改 payload、不重算向量——条文正文没变，向量不需要重算。
        if args.apply and tax_type_changes:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            from app.retrieval.collections import KB_ARTICLES
            from app.retrieval.qdrant_client import get_client

            client = get_client()
            synced = 0
            for regulation_id, _old, new_value in tax_type_changes:
                client.set_payload(
                    collection_name=KB_ARTICLES,
                    payload={TAX_TYPES_FIELD: new_value},
                    points=Filter(
                        must=[
                            FieldCondition(
                                key="regulation_id", match=MatchValue(value=regulation_id)
                            )
                        ]
                    ),
                    wait=True,
                )
                synced += 1
            print(f"向量库 payload 已同步税种：{synced} 份法规")
    finally:
        session.close()

    print()
    print(
        f"比对 {len(documents)} 份：需修复 {changed} 份"
        f"（其中税种标注 {len(tax_type_changes)} 份），未入库 {missing} 份"
    )
    if not args.apply and changed:
        print("这是预览。加 --apply 才会真正写库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

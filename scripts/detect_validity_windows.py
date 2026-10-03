"""从正文里识别"执行期限"，把失效区间写进法规（D 专题收尾）。

用法（项目根目录）：
    python scripts/detect_validity_windows.py              # 只看差异
    python scripts/detect_validity_windows.py --apply      # 真的改

为什么需要它：
  官方政策法规库**只对一部分文件标时效性**。财政部/税务总局联合发文的
  税收优惠公告，页面上根本没有时效性字段——这类文件数量不少，
  靠官方标注永远补不全。

  但这类文件有个共同点：正文里明明白白写着执行期限。
      "本公告执行期限为2023年1月1日至2024年12月31日"
      "本公告执行至2027年12月31日"
      "执行期限延长至2022年3月31日"
  这句话就是权威依据，比人工猜可靠。

做法：
  1. 把每份法规的全部条文拼起来，正则找执行期限；
  2. 取文档里**最晚**的结束日期（一份文件可能多处提到期限，
     以最后一个口径为准）；
  3. 写进条文版本的 valid_to（失效时间）和法规的 expiry_date；
  4. 同步向量库 payload 的 valid_to_ts。

为什么改 valid_to 而不是把 effect_status 改成 repealed：
  执行期限届满 ≠ 被废止。它是一个**时间区间**问题，而检索层本来就有
  时点过滤（valid_from <= 查询时点 < valid_to）。
  写成区间的好处是"按 2023 年 6 月的口径查"仍然能查到它——
  改成 repealed 就把历史口径也一起抹掉了。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

# 识别逻辑放在领域层（app/domain/validity.py），这里是纯函数、有单测；
# 脚本只负责"读写数据库"这一件事。
from app.domain.validity import find_window  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="识别执行期限并写入失效区间")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只打印差异）")
    parser.add_argument("--sleep", type=float, default=0.0, help="预留：请求间隔秒数")
    args = parser.parse_args()

    from datetime import date, datetime, timezone

    from build_vector_index import _load_env

    _load_env()

    from sqlalchemy import select

    from app.database.session import SessionLocal
    from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

    today = date.today().isoformat()
    session = SessionLocal()
    # (regulation_id, title, start_date, end_date, 状态)
    planned: list[tuple[str, str, str | None, str, str]] = []
    try:
        regulations = session.execute(select(Regulation)).scalars().all()
        for regulation in regulations:
            if regulation.effect_status not in {"effective", "partially_repealed"}:
                continue  # 已经判定废止/被替代的不用再算区间

            versions = session.execute(
                select(RegulationArticleVersion)
                .join(RegulationArticle, RegulationArticle.id == RegulationArticleVersion.article_id)
                .where(RegulationArticle.regulation_id == regulation.id)
            ).scalars().all()
            if not versions:
                continue
            # 判"要不要处理"看的是**条文版本上的 valid_to**，不是主表的 expiry_date。
            #
            # 注意：写成 `if regulation.expiry_date: continue` 会把只填了失效日期的正常文件一起跳过。
            # 结果凡是主表上被写了一个日期（哪怕那个日期其实是"生效日"）的法规，
            # 都会被永久跳过——它的条文版本永远拿不到 valid_to，
            # 而检索过滤只认版本上的时间，于是**早就过期的政策一直被当成现行有效引用**。
            # 实测：财税〔2017〕43 号（正文写明执行至 2019-12-31）在 2026 年还被引出来。
            if all(version.valid_to for version in versions):
                continue
            body = "\n".join(v.content or "" for v in versions)
            start_date, end_date = find_window(body)
            if not end_date:
                continue
            # 只有结束日、且结束日早于库里记的起始日（起始日多半是入库时间），
            # 说明这份文件的执行期整个落在过去，区间表达不了，只能判为失效。
            recorded_from = min(v.valid_from for v in versions if v.valid_from)
            entirely_past = (
                start_date is None
                and recorded_from is not None
                and end_date < recorded_from.date().isoformat()
            )
            status = "已过期" if (end_date < today or entirely_past) else "执行中"
            planned.append((regulation.id, regulation.title, start_date, end_date, status))

        expired = [item for item in planned if item[4] == "已过期"]
        running = [item for item in planned if item[4] == "执行中"]
        print(f"识别到执行期限：{len(planned)} 份（已过期 {len(expired)}、执行中 {len(running)}）\n")
        for label, items in (("已过期（默认不再作为依据）", expired), ("执行中（到期后自动失效）", running)):
            print(f"  {label}：{len(items)} 份")
            for _id, title, start_date, end_date, _status in items[:8]:
                span = f"{start_date or '?'} ~ {end_date}"
                print(f"      {span:<26} {title[:44]}")
            if len(items) > 8:
                print(f"      …… 其余 {len(items) - 8} 份")

        if not args.apply:
            print("\n这是预览。加 --apply 才会真正写库。")
            return 0
        if not planned:
            print("\n没有需要处理的。")
            return 0

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from app.retrieval.collections import KB_ARTICLES
        from app.retrieval.qdrant_client import get_client
        from app.services import audit

        client = get_client()
        for regulation_id, title, start_date, end_date, status in planned:
            regulation = session.get(Regulation, regulation_id)
            if regulation is None:
                continue
            end_moment = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
            regulation.expiry_date = end_moment

            # 只知道结束日、且执行期整个在过去：区间表达不了（valid_to 必须大于
            # valid_from），直接判为失效，避免"执行期已过"的文件继续当依据。
            entirely_past = start_date is None and status == "已过期"
            if entirely_past:
                regulation.effect_status = "repealed"

            versions = session.execute(
                select(RegulationArticleVersion)
                .join(RegulationArticle, RegulationArticle.id == RegulationArticleVersion.article_id)
                .where(RegulationArticle.regulation_id == regulation_id)
            ).scalars().all()
            for version in versions:
                if entirely_past:
                    version.effect_status = "repealed"
                    continue
                # 数据库字段是带时区的 timestamp，必须传 datetime 而不是字符串，
                # 否则 psycopg 会报 invalid input syntax for type timestamp。
                if start_date:
                    version.valid_from = datetime.fromisoformat(start_date).replace(
                        tzinfo=timezone.utc
                    )
                version.valid_to = end_moment

            # 向量库的时点过滤用 valid_to_ts（Unix 秒）
            client.set_payload(
                collection_name=KB_ARTICLES,
                payload=(
                    {"effect_status": "repealed"}
                    if entirely_past
                    else {
                        "valid_to_ts": int(end_moment.timestamp()),
                        **(
                            {
                                "valid_from_ts": int(
                                    datetime.fromisoformat(start_date)
                                    .replace(tzinfo=timezone.utc)
                                    .timestamp()
                                )
                            }
                            if start_date
                            else {}
                        ),
                    }
                ),
                points=Filter(
                    must=[
                        FieldCondition(key="regulation_id", match=MatchValue(value=regulation_id))
                    ]
                ),
                wait=True,
            )

        # 判为失效的，把索引点删掉（与检索白名单口径一致）
        from app.knowledge.ontology import CITABLE_EFFECT_STATUSES
        from qdrant_client.models import MatchAny

        client.delete(
            collection_name=KB_ARTICLES,
            points_selector=Filter(
                must_not=[
                    FieldCondition(
                        key="effect_status", match=MatchAny(any=list(CITABLE_EFFECT_STATUSES))
                    )
                ]
            ),
            wait=True,
        )

        audit.record(
            session,
            action="knowledge.validity_windows_detected",
            actor_id=None,
            actor_username="system:detect_validity_windows",
            target_type="regulation_batch",
            detail={"detected": len(planned), "expired": len(expired)},
        )
        session.commit()
        print(f"\n已写入 {len(planned)} 份法规的执行期限（其中 {len(expired)} 份已过期）")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""补齐缺失的官方时效性（D 专题收尾）。

用法（项目根目录）：
    python scripts/backfill_aging.py            # 抓取并写缓存
    python scripts/backfill_aging.py --limit 20 # 先抓 20 份试水

背景：
  效力状态来自官方政策法规库的"时效性"（全文有效 / 已修改 / 全文废止……）。
  采集时把它写进了 sidecar，但**最早那批数据没有这个字段**——
  早期导入的 34 份、以及早期采集脚本写入的 10 份，共 100 多份法规查不到时效性，
  只能按"现行有效"处理。这属于"不知道"，不是"已确认有效"。

做法：
  对那些没有 sidecar 记录的法规，直接打开它的官方详情页，
  从页面上的 `<span class="xg">全文有效</span>` 里读时效性。
  结果写进缓存文件 `法规数据/_official_aging.json`，
  供 scripts/audit_validity.py 复用（避免每次都重新抓一遍）。

只处理来源是政策法规库（fgk.chinatax.gov.cn）的法规。
其余来源（中国政府网、人大法规库、财政部）页面结构不同，且多为法律，
不做自动核查，在结尾一并列出，交人工确认。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.request

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

CACHE_FILE = REPO / "法规数据" / "_official_aging.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 详情页上的时效性：<p class="arc_date"><span class="xg">全文有效</span> ...
AGING_RE = re.compile(r'<span class="xg"[^>]*>([^<]{1,12})</span>')


def _normalize(title: str) -> str:
    return re.sub(r"[\s（）()《》〔〕\[\]—\-－_、，,。.]+", "", title or "")


def load_cache() -> dict[str, dict]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def fetch_aging(url: str, retries: int = 2) -> str | None:
    """从详情页读时效性。取不到返回 None。"""

    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                html = response.read().decode("utf-8", errors="replace")
            match = AGING_RE.search(html)
            return match.group(1).strip() if match else None
        except Exception:  # noqa: BLE001 - 单份抓不到不影响整批
            time.sleep(1.5 * (attempt + 1))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐官方时效性")
    parser.add_argument("--limit", type=int, default=None, help="最多抓几份（试水用）")
    parser.add_argument("--sleep", type=float, default=0.4, help="每次请求间隔秒数")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from sqlalchemy import select

    from app.database.session import SessionLocal
    from app.models.knowledge import Regulation

    # 已经有时效性的（sidecar 里有的）不用再抓
    known: set[str] = set()
    for directory in REPO.glob("法规数据/待导入*"):
        for meta_file in directory.glob("*.meta.json"):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if (meta.get("effect_status_hint") or "").strip():
                known.add(_normalize(meta.get("title") or ""))

    cache = load_cache()
    session = SessionLocal()
    fetched = 0
    failed: list[str] = []
    other_source: list[tuple[str, str]] = []
    try:
        regulations = session.execute(select(Regulation)).scalars().all()
        pending = [
            item
            for item in regulations
            if _normalize(item.title) not in known
            and _normalize(item.title) not in cache
            and "fgk.chinatax.gov.cn" in (item.source_url or "")
        ]
        if args.limit:
            pending = pending[: args.limit]

        print(f"待补查：{len(pending)} 份（来源为政策法规库）\n")
        for index, regulation in enumerate(pending, start=1):
            aging = fetch_aging(regulation.source_url)
            if aging:
                cache[_normalize(regulation.title)] = {"aging": aging, "source": "detail_page"}
                fetched += 1
                print(f"  [{index}/{len(pending)}] {aging}  {regulation.title[:44]}")
            else:
                failed.append(regulation.title)
                print(f"  [{index}/{len(pending)}] 未取到      {regulation.title[:44]}")
            time.sleep(args.sleep)

        # 非政策法规库来源的，列出来交人工
        for regulation in regulations:
            if _normalize(regulation.title) in known or _normalize(regulation.title) in cache:
                continue
            if "fgk.chinatax.gov.cn" not in (regulation.source_url or ""):
                other_source.append((regulation.title, regulation.source_url or "(无)"))
    finally:
        session.close()

    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\n已抓取 {fetched} 份，未取到 {len(failed)} 份，缓存写入 {CACHE_FILE.name}")

    if other_source:
        print(f"\n以下 {len(other_source)} 份来源不是政策法规库，需人工确认时效性：")
        for title, url in other_source[:25]:
            print(f"  · {title[:44]}  （{url[:52]}）")
        if len(other_source) > 25:
            print(f"  …… 其余 {len(other_source) - 25} 份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

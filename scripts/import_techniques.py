"""把筹划手法素材导入数据库。

用法（项目根目录）：
    python scripts/import_techniques.py --review-state draft

幂等：按 code 更新，可反复执行。

**手法素材默认以 draft（待专家确认）状态入库**——
技术方案 4.11 的六字段里，"滥用边界"和"被否案例"必须由财税专家把关。
入库不等于可用：出方案时只认 published 状态的手法。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


def main() -> int:
    parser = argparse.ArgumentParser(description="导入筹划手法")
    parser.add_argument(
        "--review-state",
        default="draft",
        choices=["draft", "published"],
        help="入库状态，默认 draft（待专家确认）",
    )
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal
    from app.planning.techniques import import_techniques, load_technique_file

    items = load_technique_file()
    print(f"待导入手法：{len(items)} 条")
    for item in items:
        print(f"  · [{item['risk_level']}] {item['name']}（{item['category']}）")

    session = SessionLocal()
    try:
        stats = import_techniques(session, review_state=args.review_state)
    finally:
        session.close()
    print(f"\n完成：新增 {stats['created']}，更新 {stats['updated']}，状态 {args.review_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

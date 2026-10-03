"""法规导入脚本：把一个目录里的法规文件批量导入数据库。

用法（项目根目录）：
    python scripts/import_regulations.py --inbox <目录>
    python scripts/import_regulations.py --inbox <目录> --domain finance_tax

支持的文件类型：.txt / .md（UTF-8、GBK、GB18030、Big5 自动识别）

导入规则（与系统内导入流水线一致）：
  · 一份文件一个独立事务，失败不影响其它文件
  · 同文号 + 同位阶的文件视为重复，自动跳过（可反复运行）
  · 缺文号 / 缺施行日期 / 缺来源的文件照样入库，但标 pending_review 等人工确认

退出码：0 = 全部成功或跳过；1 = 有文件失败（失败清单打印在末尾）
"""

from __future__ import annotations
import argparse
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description="批量导入财税法规")
    parser.add_argument("--inbox", required=True, help="存放法规文件的目录")
    parser.add_argument("--domain", default="finance_tax", help="域包标识，默认 finance_tax")
    args = parser.parse_args()

    # 环境变量要在导入 app.config 之前设好，否则配置会被缓存成开发库
    import os

    env_file = REPO / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

    # .env 里写的是容器内主机名（postgres / qdrant …），
    # 本机直接跑脚本时解析不了，要换成 127.0.0.1（Docker 端口已映射到本机）
    db_url = os.environ.get("DATABASE_URL", "")
    for container_host in ("@postgres:", "@qdrant:", "@redis:", "@embedding:"):
        if container_host in db_url:
            db_url = db_url.replace(container_host, "@127.0.0.1:")
            break
    if db_url:
        os.environ["DATABASE_URL"] = db_url

    from app.database.session import SessionLocal
    from app.services.ingest.collector import collect_from_directory
    from app.services.ingest.importer import import_documents

    inbox = pathlib.Path(args.inbox).resolve()
    if not inbox.exists():
        print(f"[错误] 目录不存在：{inbox}")
        return 1

    documents = collect_from_directory(inbox, domain_id=args.domain)
    if not documents:
        print(f"[提示] {inbox} 里没有可导入的文件（支持 .txt / .md）")
        return 0

    print(f"发现 {len(documents)} 份待导入文件，开始导入……\n")

    def show_progress(current: int, total: int, filename: str, status: str) -> None:
        percent = round(current / total * 100, 1)
        print(f"  [{current}/{total}] {percent:5.1f}%  {filename}  → {status}")

    session = SessionLocal()
    try:
        report = import_documents(
            session, documents, domain_id=args.domain, on_progress=show_progress
        )
    finally:
        session.close()

    print("\n" + "=" * 60)
    print(f"总文件数    ：{report.total}")
    print(f"导入成功    ：{report.success_count}")
    print(f"跳过（重复）：{report.skipped_count}")
    print(f"导入失败    ：{report.failed_count}")
    print(f"待人工复核  ：{report.review_count}")
    print(f"写入条文数  ：{report.article_count}")

    if report.review_items:
        print("\n待人工复核清单（这些文件缺文号、缺施行日期或缺来源）：")
        for item in report.review_items:
            print(f"  · {item['filename']}  {item['title']}")
            print(f"    原因：{item['reasons']}")

    if report.failures:
        print("\n失败清单：")
        for item in report.failures:
            print(f"  · {item['filename']}：{item['reason']}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

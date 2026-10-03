"""初始化数据库：执行 Alembic 迁移到最新版本。

用法（项目根目录，宿主机）：
    python scripts/init_db.py
用法（项目根目录，容器内，无需本机装 Python）：
    docker compose exec backend alembic upgrade head

尚无业务表，执行成功即代表迁移链路可用。

注意：DATABASE_URL 默认从 backend/.env 或环境变量读取。宿主机执行时需要指向本机端口，
例如先设置 DATABASE_URL=postgresql+psycopg://cs_user:cs_password@127.0.0.1:5432/customer_service。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"


def main() -> int:
    if not BACKEND_DIR.exists():
        print(f"找不到 backend 目录：{BACKEND_DIR}")
        return 1

    print(f"在 {BACKEND_DIR} 执行：alembic upgrade head")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        check=False,
    )
    if result.returncode != 0:
        print("迁移失败。常见原因：数据库未启动，或 DATABASE_URL 指向错误。")
        return result.returncode

    print("迁移完成 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

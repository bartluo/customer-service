"""重置固定管理员口令（忘记密码时的兜底手段）。

用法（项目根目录）：
    # 宿主机（需先设好 DATABASE_URL 指向本机端口）
    python scripts/reset_admin_password.py
    # 容器内（推荐，数据库地址已配好）
    docker compose exec backend python /app/../scripts/reset_admin_password.py

为什么需要单独脚本：固定管理员不能被别人重置口令（技术方案 10.4），
所以唯一的自救路径就是拿服务器权限执行这个脚本。这本身就是最高门槛。
"""

from __future__ import annotations
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

# 兼容两种布局：
#   仓库本机运行：scripts/ 的上一级是仓库根，backend 包在 backend/ 下
#   容器内运行：脚本在 /app/scripts/，backend 包直接在 /app/ 下
_script_dir = pathlib.Path(__file__).resolve().parent
_repo_root = _script_dir.parent
for _candidate in (_repo_root / "backend", _repo_root):
    if (_candidate / "app" / "config.py").exists():
        sys.path.insert(0, str(_candidate))
        break

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.security.passwords import generate_password, hash_password  # noqa: E402
from app.services.seed import run_seeds  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        run_seeds(db, get_settings())
        admin = db.execute(select(User).where(User.is_protected.is_(True))).scalar_one_or_none()
        if admin is None:
            print("没有找到固定管理员，请先启动一次后端（启动时会自动创建）")
            return 1
        password = generate_password()
        admin.password_hash = hash_password(password)
        admin.must_change_password = True
        db.commit()
        print("=" * 60)
        print(f"固定管理员 {admin.username} 的口令已重置为：{password}")
        print("请立即登录并在首次提示时修改该口令。")
        print("=" * 60)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

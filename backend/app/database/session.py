"""数据库连接与会话管理。"""

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# psycopg 的 connect_timeout 以秒为单位且必须是整数
_connect_args: dict[str, object] = {}
if settings.database_url.startswith("postgresql"):
    _connect_args["connect_timeout"] = max(1, int(settings.db_connect_timeout_seconds))

# pool_pre_ping：连接池取出的连接先探活，避免数据库重启后拿到死连接
engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args=_connect_args,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话，请求结束自动关闭。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_connection(timeout_seconds: float = 3.0) -> None:
    """探活：连不上或查询失败会抛异常。"""

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

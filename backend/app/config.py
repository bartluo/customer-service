"""应用配置：全部来自环境变量，便于 SaaS 与私有化两种形态复用同一份代码。"""

import pathlib
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。字段名对应环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_name: str = "财税知识引擎"
    environment: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api"

    # ---------- 依赖服务 ----------
    database_url: str = "postgresql+psycopg://cs_user:cs_password@localhost:5432/customer_service"
    qdrant_url: str = "http://localhost:6333"
    redis_url: str = "redis://localhost:6379/0"
    embedding_url: str = "http://localhost:8001"

    # ---------- 向量维度 ----------
    dense_dim: int = 1024

    # ---------- 域包 ----------
    # 域包根目录。容器内 /app/domains（由 Dockerfile 复制），本机为仓库根的 domains/
    domains_root: str = "../domains"
    # 启动时若域包加载失败，是否让整个服务起不来。
    # 开发期 True（写错 manifest 要立刻报错），生产期 False（一个域坏掉不该拖垮全站）。
    domain_pack_strict: bool = True

    # ---------- LLM（起使用，允许为空） ----------
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_fallback_model: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""

    # ---------- 安全 ----------
    jwt_secret: str = "change-me-in-production"
    jwt_expire_minutes: int = 1440
    jwt_algorithm: str = "HS256"
    # 固定管理员（唯一超级账号）。口令留空时，首次启动生成随机口令并打印到日志。
    bootstrap_admin_username: str = "root_admin"
    bootstrap_admin_password: str = ""
    # 生产环境必须显式设置强口令，否则启动时拒绝运行（避免默认口令进生产）。
    bootstrap_admin_password_min_length: int = 12
    cors_origins: str = "http://localhost:3000"

    # ---------- 功能开关 ----------
    # 筹划功能：资质主体（F2）落实前保持关闭
    feature_planning_enabled: bool = False

    # ---------- 健康检查 ----------
    health_check_timeout_seconds: float = Field(default=3.0, gt=0)
    # 数据库连接超时（秒）。必须显式设置：psycopg 默认跟随操作系统，
    # 在网络丢包（而非拒绝连接）的环境下可能等待数十秒，拖死健康检查。
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_test(self) -> bool:
        return self.environment.lower() == "test"


@lru_cache
def get_settings() -> Settings:
    """进程内缓存配置，避免每次请求都重新解析环境变量。"""

    return Settings()


def domain_file(*parts: str) -> pathlib.Path:
    """定位域包里的文件，如 domain_file("finance_tax", "templates.yaml")。

    为什么不能各模块自己算相对路径：本机跑脚本时，`backend/app/xxx.py` 往上
    数三层正好是仓库根；但代码进了容器是 `/app/app/xxx.py`，往上数三层是根目录
    `/`，域包文件其实在 `/app/domains`。同一条 `parents[3]` 在两种环境里
    指向两个地方——本机全对、容器全错，而且要等到第一次在容器里跑推理链路才暴露。
    （第一次从容器调问答接口就是这么炸的。）

    规则：先按配置的 `domains_root`（容器内由 .env 指定 /app/domains）找；
    找不到再退回"仓库根/domains"（本机直接跑脚本、且 .env 里的容器路径不适用时）。
    """

    relative = pathlib.Path(*parts)
    configured = (pathlib.Path(get_settings().domains_root) / relative).resolve()
    if configured.exists():
        return configured
    # config.py 在 backend/app/ 下，往上两层是仓库根
    return (pathlib.Path(__file__).resolve().parents[2] / "domains" / relative).resolve()

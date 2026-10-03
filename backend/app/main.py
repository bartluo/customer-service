"""FastAPI 应用入口。"""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app import __version__
from app.config import get_settings
from app.logging_config import configure_logging
from app.routers import admin, auth, calc, console, domains, health, knowledge, planning, qa

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level, json_output=settings.is_production)
    logger.info(
        "服务启动 environment=%s version=%s planning_enabled=%s",
        settings.environment,
        __version__,
        settings.feature_planning_enabled,
    )
    _bootstrap_accounts()
    _bootstrap_domain_packs()
    yield
    logger.info("服务停止")


def _bootstrap_accounts() -> None:
    """启动时确保权限项、内置角色与固定管理员存在（幂等）。

    为什么放在启动流程里：部署到新机器时不应该还要记得"记得先跑一遍种子脚本"。
    幂等保证重启不会重复插入。
    """

    from app.database.session import SessionLocal
    from app.services.seed import run_seeds

    db = SessionLocal()
    try:
        result = run_seeds(db, settings)
        if result["protected_admin_created"]:
            logger.warning(
                "已创建固定管理员 username=%s 初始口令=%s（请立即登录并修改）",
                result["protected_admin_username"],
                result["initial_password"],
            )
        else:
            logger.info(
                "账号基线就绪 admin=%s 新增权限=%s 新增角色=%s",
                result["protected_admin_username"],
                result["permissions_created"],
                result["roles_created"],
            )
    except Exception:  # noqa: BLE001 - 启动期兜底，避免因账号初始化失败导致健康检查全挂
        db.rollback()
        logger.exception("账号基线初始化失败，登录与授权功能暂不可用")
    finally:
        db.close()


def _bootstrap_domain_packs() -> None:
    """启动时加载域包。

    严格模式下（开发期）加载失败直接抛异常，让容器起不来——
    域包没加载还继续跑，问答时会返回空结果，问题会被藏起来。
    """

    from pathlib import Path

    from app.domain_packs import DomainPackError, get_registry

    domains_root = Path(settings.domains_root).resolve()
    try:
        count = get_registry().load(domains_root)
        logger.info("域包加载完成：%d 个（目录 %s）", count, domains_root)
    except DomainPackError:
        if settings.domain_pack_strict:
            logger.exception("域包加载失败，且 domain_pack_strict=true，服务拒绝启动")
            raise
        logger.exception("域包加载失败，已跳过（domain_pack_strict=false），相关功能不可用")


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="垂直领域知识引擎 —— 财税先行。：账号与权限。",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)
app.include_router(domains.router, prefix=settings.api_prefix)
app.include_router(knowledge.router, prefix=settings.api_prefix)
# 的对外接口（技术方案 12.4）。统一挂在 /api/v1/ 下：
# 版本号是给外部调用方用的——他们有升级周期，不能跟着我们改一次就跟着改。
app.include_router(qa.router, prefix=settings.api_prefix)
app.include_router(qa.citations_router, prefix=settings.api_prefix)
app.include_router(calc.router, prefix=settings.api_prefix)
app.include_router(planning.router, prefix=settings.api_prefix)
app.include_router(console.router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """根路径直接跳到接口文档，方便浏览器访问。"""

    return RedirectResponse(url="/docs")

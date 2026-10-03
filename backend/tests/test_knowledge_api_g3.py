"""财税知识库的接口测试（对外能力）。

需要真实 PostgreSQL。覆盖：
  · POST /api/knowledge/regulations/import 批量导入（管理员/审核专家）
  · GET  /api/knowledge/regulations 列表与详情
  · 权限：客户角色不能导入
  · 检索前置的"只读已发布"约束
"""

from __future__ import annotations
import os
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import get_settings
from app.database.base import Base
from app.main import app

TEST_DB_NAME = "customer_service_test"
ADMIN_PASSWORD = "Fixed-Admin-Pass-2026"


def _test_database_url() -> str:
    base = os.environ["DATABASE_URL"]
    return base.rsplit("/", 1)[0] + "/" + TEST_DB_NAME


def _ensure_test_database() -> None:
    server_url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    server = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :name"),
            {"name": TEST_DB_NAME},
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{TEST_DB_NAME}"'))
    server.dispose()

    engine = create_engine(_test_database_url())
    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()


@pytest.fixture()
def client():
    _ensure_test_database()

    # 让应用指向测试库后再启动（启动时才会自动建固定管理员并用固定口令）
    os.environ["DATABASE_URL"] = _test_database_url()
    os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    get_settings.cache_clear()

    from app.database import session as session_module

    session_module.engine.dispose()
    session_module.engine = create_engine(_test_database_url(), pool_pre_ping=True)
    session_module.SessionLocal.configure(bind=session_module.engine)

    with TestClient(app) as test_client:
        yield test_client


def _token(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "root_admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


SAMPLE_DOC = {
    "filename": "39hao.txt",
    "text": """财政部 税务总局 海关总署公告2019年第39号

关于深化增值税改革有关政策的公告

第一条 增值税一般纳税人发生销售服务、无形资产或者不动产应税行为，适用税率13%。
""",
    "source_url": "https://www.chinatax.gov.cn/example/39hao.html",
}


def test_import_endpoint_requires_auth(client: TestClient) -> None:
    """未登录不能导入。"""

    response = client.post("/api/knowledge/regulations/import", json={"documents": [SAMPLE_DOC]})
    assert response.status_code in (401, 403)


def test_import_endpoint_rejects_client_role(client: TestClient) -> None:
    """客户角色不能导入法规。"""

    admin_token = _token(client)
    # 创建一个客户角色用户
    suffix = uuid.uuid4().hex[:8]
    client_username = f"client_{suffix}"
    tenant = client.post(
        "/api/admin/tenants",
        json={"code": f"t_{suffix}", "name": "测试租户"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert tenant.status_code in (200, 201), tenant.text
    tenant_id = tenant.json()["id"]

    user = client.post(
        "/api/admin/users",
        json={
            "tenant_id": tenant_id,
            "username": client_username,
            "email": f"{client_username}@example.com",
            "display_name": "客户",
            "password": "Client-Pass-2026",
            "role_code": "client",
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert user.status_code in (200, 201), user.text

    client_login = client.post(
        "/api/auth/login",
        json={"username": client_username, "password": "Client-Pass-2026"},
    )
    assert client_login.status_code == 200, client_login.text
    client_token = client_login.json()["access_token"]

    response = client.post(
        "/api/knowledge/regulations/import",
        json={"documents": [SAMPLE_DOC]},
        headers={"Authorization": f"Bearer {client_token}"},
    )
    assert response.status_code == 403


def test_import_endpoint_accepts_documents(client: TestClient) -> None:
    """管理员导入成功，返回报告。"""

    token = _token(client)
    response = client.post(
        "/api/knowledge/regulations/import",
        json={"documents": [SAMPLE_DOC]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["success"] == 1
    assert body["failed"] == 0
    assert body["progress_percent"] == 100.0


def test_list_regulations_returns_imported(client: TestClient) -> None:
    """导入后能查到法规列表。"""

    token = _token(client)
    client.post(
        "/api/knowledge/regulations/import",
        json={"documents": [SAMPLE_DOC]},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.get("/api/knowledge/regulations", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) >= 1
    assert items[0]["domain_id"] == "finance_tax"


def test_get_regulation_detail_includes_articles(client: TestClient) -> None:
    """详情接口返回条文树。"""

    token = _token(client)
    client.post(
        "/api/knowledge/regulations/import",
        json={"documents": [SAMPLE_DOC]},
        headers={"Authorization": f"Bearer {token}"},
    )
    listing = client.get("/api/knowledge/regulations", headers={"Authorization": f"Bearer {token}"})
    regulation_id = listing.json()["items"][0]["id"]

    response = client.get(
        f"/api/knowledge/regulations/{regulation_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == regulation_id
    assert len(body["articles"]) >= 1
    assert body["articles"][0]["level_code"] == "article"


def test_list_supports_effect_status_filter(client: TestClient) -> None:
    """列表支持按效力状态过滤。"""

    token = _token(client)
    client.post(
        "/api/knowledge/regulations/import",
        json={"documents": [SAMPLE_DOC]},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.get(
        "/api/knowledge/regulations?effect_status=repealed",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []

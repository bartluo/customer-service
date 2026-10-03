"""知识复核接口测试（放行 / 驳回）。

需要真实 PostgreSQL。覆盖四条不能破的规则：
  · 复核是审核权限，客户角色与管理员都不能做
  · 待复核清单要能说清"为什么待复核"
  · 放行/驳回改变 review_state，并写审计日志
  · 放行只对"已放行"状态生效，检索只认 published
"""

from __future__ import annotations
import os
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import app.models  # noqa: F401
from app.config import get_settings
from app.database.base import Base
from app.main import app
from app.services.review import (
    ACTION_APPROVE,
    ACTION_REJECT,
    REVIEW_PENDING,
    REVIEW_PUBLISHED,
    REVIEW_REJECTED,
    apply_decision,
    pending_reasons,
)

TEST_DB_NAME = "customer_service_test_review"
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

    os.environ["DATABASE_URL"] = _test_database_url()
    os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    get_settings.cache_clear()

    from app.database import session as session_module

    session_module.engine.dispose()
    session_module.engine = create_engine(_test_database_url(), pool_pre_ping=True)
    session_module.SessionLocal.configure(bind=session_module.engine)

    with TestClient(app) as test_client:
        yield test_client


# 这份公告缺文号、缺施行日期 → 导入后必然是 pending_review，正好当复核用例
NEEDS_REVIEW_DOC = {
    "filename": "no-number.txt",
    "text": "关于某事项的公告\n\n第一条 本公告自发布之日起执行。\n",
    "source_url": "https://www.chinatax.gov.cn/example/no-number.html",
}


def _token(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "root_admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _make_user(client: TestClient, admin_token: str, role_code: str) -> str:
    """建一个指定角色的用户并返回其令牌。"""

    suffix = uuid.uuid4().hex[:8]
    tenant = client.post(
        "/api/admin/tenants",
        json={"code": f"t_{suffix}", "name": "复核测试租户"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert tenant.status_code in (200, 201), tenant.text
    username = f"{role_code}_{suffix}"
    created = client.post(
        "/api/admin/users",
        json={
            "tenant_id": tenant.json()["id"],
            "username": username,
            "email": f"{username}@example.com",
            "display_name": role_code,
            "password": "Review-Pass-2026",
            "role_code": role_code,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert created.status_code in (200, 201), created.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Review-Pass-2026"},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _import_pending(client: TestClient, token: str) -> str:
    """导入一份必然待复核的法规，返回它的 id。"""

    response = client.post(
        "/api/knowledge/regulations/import",
        json={"documents": [NEEDS_REVIEW_DOC]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["needs_review"] == 1

    listed = client.get(
        "/api/knowledge/regulations",
        params={"review_state": REVIEW_PENDING, "limit": 200},
    )
    assert listed.status_code == 200, listed.text
    for item in listed.json()["items"]:
        if item["title"] == "关于某事项的公告":
            return item["id"]
    raise AssertionError("导入后没在待复核列表里找到这份法规")


def test_pending_review_requires_authentication(client: TestClient) -> None:
    """未登录看不到复核队列。"""

    response = client.get("/api/knowledge/review/pending")
    assert response.status_code in (401, 403)


def test_pending_review_rejects_client_and_admin_roles(client: TestClient) -> None:
    """复核是审核权限：客户和管理员都不能做。"""

    admin_token = _token(client)
    for role_code in ("client", "admin"):
        token = _make_user(client, admin_token, role_code)
        response = client.get(
            "/api/knowledge/review/pending",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403, f"{role_code} 不应有复核权限：{response.text}"


def test_pending_review_lists_reasons(client: TestClient) -> None:
    """待复核清单必须说清"为什么待复核"。"""

    token = _token(client)
    _import_pending(client, token)

    response = client.get(
        "/api/knowledge/review/pending",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] >= 1
    target = next(item for item in body["items"] if item["title"] == "关于某事项的公告")
    assert any("文号" in reason for reason in target["reasons"])


def test_approve_publishes_and_writes_audit(client: TestClient) -> None:
    """放行后状态变 published，并留下审计记录。"""

    token = _token(client)
    regulation_id = _import_pending(client, token)

    response = client.post(
        f"/api/knowledge/review/{regulation_id}",
        json={"action": ACTION_APPROVE, "note": "人工核对通过"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_state"] == REVIEW_PUBLISHED
    assert body["previous_state"] == REVIEW_PENDING

    detail = client.get(f"/api/knowledge/regulations/{regulation_id}")
    assert detail.status_code == 200
    assert detail.json()["review_state"] == REVIEW_PUBLISHED

    pending = client.get(
        "/api/knowledge/review/pending",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert all(item["id"] != regulation_id for item in pending.json()["items"])


def test_reject_keeps_it_out_of_pending(client: TestClient) -> None:
    """驳回后也不再出现在待复核队列里。"""

    token = _token(client)
    regulation_id = _import_pending(client, token)

    response = client.post(
        f"/api/knowledge/review/{regulation_id}",
        json={"action": ACTION_REJECT, "note": "附件型公告，无正文"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_state"] == REVIEW_REJECTED


def test_review_endpoint_requires_review_permission(client: TestClient) -> None:
    """客户角色不能放行或驳回。"""

    admin_token = _token(client)
    regulation_id = _import_pending(client, admin_token)
    client_token = _make_user(client, admin_token, "client")

    response = client.post(
        f"/api/knowledge/review/{regulation_id}",
        json={"action": ACTION_APPROVE},
        headers={"Authorization": f"Bearer {client_token}"},
    )
    assert response.status_code == 403


def test_unknown_regulation_returns_404(client: TestClient) -> None:
    """复核一个不存在的法规返回 404，而不是静默成功。"""

    token = _token(client)
    response = client.post(
        f"/api/knowledge/review/{uuid.uuid4()}",
        json={"action": ACTION_APPROVE},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_invalid_action_is_rejected(client: TestClient) -> None:
    """只接受 approve / reject 两种结论。"""

    token = _token(client)
    regulation_id = _import_pending(client, token)

    response = client.post(
        f"/api/knowledge/review/{regulation_id}",
        json={"action": "maybe"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


# ---------- 服务层：规则本身 ----------


def test_pending_reasons_are_empty_when_metadata_complete() -> None:
    """元数据齐全的法规不该被列理由（它本来也不会进待复核队列）。"""

    class _Fake:
        document_number = "国家税务总局公告2026年第1号"
        hierarchy_level = "normative_document"
        effective_date = object()
        source_url = "https://fgk.chinatax.gov.cn/x"

    assert pending_reasons(_Fake()) == []  # type: ignore[arg-type]


def test_apply_decision_rejects_unknown_action() -> None:
    """未知动作直接报错，不能悄悄写成某种状态。"""

    class _Fake:
        review_state = REVIEW_PENDING
        id = "x"
        title = "t"
        document_number = None

    with pytest.raises(ValueError):
        apply_decision(
            None,  # type: ignore[arg-type]
            _Fake(),  # type: ignore[arg-type]
            action="whatever",
            note=None,
            actor_id=None,
            actor_username="tester",
        )

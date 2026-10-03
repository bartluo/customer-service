"""账号与权限的端到端测试（需要真实的 PostgreSQL）。

覆盖技术方案 10.4 / 10.5 的硬性要求：
  · 固定管理员不可删除、不可停用、不可降权
  · 普通管理员不能授予审核专家角色、不能转授不可转授的权限
  · 授权立刻生效（不需重新登录）
  · 越权访问 100% 被拒（401/403）

为什么单独建库：这些测试会真实建租户与用户，污染开发库。测试自己建一个
customer_service_test 库，不影响开发数据。
"""

from __future__ import annotations
import os
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.config import get_settings
from app.database.base import Base
from app.main import app
from app.models import User

TEST_DB_NAME = "customer_service_test"
ADMIN_BOOTSTRAP_PASSWORD = "Fixed-Admin-Pass-2026"


def _test_database_url() -> str:
    base = os.environ["DATABASE_URL"]
    return base.rsplit("/", 1)[0] + "/" + TEST_DB_NAME


def _ensure_test_database() -> None:
    base = os.environ["DATABASE_URL"]
    server_url = base.rsplit("/", 1)[0] + "/postgres"
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :name"),
            {"name": TEST_DB_NAME},
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{TEST_DB_NAME}"'))
    engine.dispose()


@pytest.fixture(scope="module")
def client():
    _ensure_test_database()
    engine = create_engine(_test_database_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()

    # 让应用指向测试库后再启动（启动时会自动建种子与固定管理员）
    os.environ["DATABASE_URL"] = _test_database_url()
    os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ADMIN_BOOTSTRAP_PASSWORD
    os.environ["JWT_SECRET"] = "test-secret-for-account-tests"
    get_settings.cache_clear()

    from app.database import session as session_module

    session_module.engine.dispose()
    session_module.engine = create_engine(_test_database_url(), pool_pre_ping=True)
    session_module.SessionLocal.configure(bind=session_module.engine)

    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "root_admin", ADMIN_BOOTSTRAP_PASSWORD)


def _create_tenant(client: TestClient, headers: dict[str, str], name: str) -> dict:
    response = client.post(
        "/api/admin/tenants",
        headers=headers,
        json={"code": f"t-{uuid.uuid4().hex[:8]}", "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_user(
    client: TestClient,
    headers: dict[str, str],
    *,
    role_code: str,
    tenant_id: str | None = None,
    password: str = "Initial-Pass-2026",
) -> dict:
    suffix = uuid.uuid4().hex[:6]
    body = {
        "username": f"u{suffix}",
        "email": f"u{suffix}@example.com",
        "display_name": f"测试用户{suffix}",
        "password": password,
        "role_code": role_code,
    }
    if tenant_id:
        body["tenant_id"] = tenant_id
    response = client.post("/api/admin/users", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["user"]


@pytest.fixture(scope="module")
def tenant_a(client: TestClient, admin_headers: dict[str, str]) -> dict:
    return _create_tenant(client, admin_headers, "甲公司")


@pytest.fixture(scope="module")
def tenant_b(client: TestClient, admin_headers: dict[str, str]) -> dict:
    return _create_tenant(client, admin_headers, "乙公司")


def test_login_reports_identity(client: TestClient, admin_headers: dict[str, str]) -> None:
    body = client.get("/api/auth/me", headers=admin_headers).json()
    assert body["user"]["username"] == "root_admin"
    assert body["user"]["is_protected"] is True


def test_login_with_wrong_password_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login", json={"username": "root_admin", "password": "wrong-password"}
    )
    assert response.status_code == 401
    # 失败信息不能泄露账号是否存在
    assert response.json()["detail"] == "用户名或口令错误"


def test_unauthenticated_access_is_rejected(client: TestClient) -> None:
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/audit-logs").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_forged_token_is_rejected(client: TestClient) -> None:
    headers = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.bad-signature"}
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_protected_admin_cannot_be_deleted(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    me = client.get("/api/auth/me", headers=admin_headers).json()["user"]
    response = client.delete(f"/api/admin/users/{me['id']}", headers=admin_headers)
    assert response.status_code == 403
    assert "固定管理员" in response.json()["detail"]


def test_protected_admin_cannot_be_suspended(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    me = client.get("/api/auth/me", headers=admin_headers).json()["user"]
    response = client.patch(
        f"/api/admin/users/{me['id']}", headers=admin_headers, json={"is_active": False}
    )
    assert response.status_code == 403


def test_protected_admin_roles_cannot_be_revoked(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    me = client.get("/api/auth/me", headers=admin_headers).json()["user"]
    response = client.delete(
        f"/api/admin/users/{me['id']}/roles/admin", headers=admin_headers
    )
    assert response.status_code == 403


def test_permission_registry_matches_spec(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.get("/api/admin/permissions", headers=admin_headers)
    assert response.status_code == 200
    codes = {item["code"] for item in response.json()}
    for expected in [
        "tenant.manage",
        "user.manage",
        "permission.grant",
        "audit.view",
        "knowledge.review",
        "planning.review.high_risk",
        "qa.ask",
        "calc.run",
        "calc.draft.save",
        "answer.export",
    ]:
        assert expected in codes, f"缺少权限码 {expected}"


def test_three_builtin_roles_exist(client: TestClient, admin_headers: dict[str, str]) -> None:
    response = client.get("/api/admin/roles", headers=admin_headers)
    assert response.status_code == 200
    roles = {item["code"]: item for item in response.json()}
    assert set(roles) == {"admin", "reviewer", "client"}
    assert roles["reviewer"]["grantable_by_admin"] is False
    # 管理员默认不含知识审核权（避免既当裁判又当运动员）
    assert "knowledge.review" not in roles["admin"]["permissions"]
    assert "knowledge.review" in roles["reviewer"]["permissions"]


def test_client_role_requires_tenant(client: TestClient, admin_headers: dict[str, str]) -> None:
    suffix = uuid.uuid4().hex[:6]
    response = client.post(
        "/api/admin/users",
        headers=admin_headers,
        json={
            "username": f"nc{suffix}",
            "email": f"nc{suffix}@example.com",
            "display_name": "无租户客户",
            "password": "Client-Pass-2026",
            "role_code": "client",
        },
    )
    assert response.status_code == 400


def test_regular_admin_cannot_grant_reviewer_role(
    client: TestClient, admin_headers: dict[str, str], tenant_a: dict
) -> None:
    """普通管理员想给审核专家角色 → 必须被拒；固定管理员授则可以。"""

    plain = _create_user(client, admin_headers, role_code="admin", password="Admin-Pass-2026")
    plain_headers = _login(client, plain["username"], "Admin-Pass-2026")
    target = _create_user(
        client, admin_headers, role_code="client", tenant_id=tenant_a["id"],
        password="Client-Pass-2026",
    )

    denied = client.post(
        f"/api/admin/users/{target['id']}/roles",
        headers=plain_headers,
        json={"role_code": "reviewer"},
    )
    assert denied.status_code == 403
    assert "固定管理员" in denied.json()["detail"]

    allowed = client.post(
        f"/api/admin/users/{target['id']}/roles",
        headers=admin_headers,
        json={"role_code": "reviewer"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["applied"] is True

    client.delete(f"/api/admin/users/{target['id']}", headers=admin_headers)
    client.delete(f"/api/admin/users/{plain['id']}", headers=admin_headers)


def test_regular_admin_cannot_grant_non_delegable_permission(
    client: TestClient, admin_headers: dict[str, str], tenant_a: dict
) -> None:
    plain = _create_user(client, admin_headers, role_code="admin", password="Admin2-Pass-2026")
    plain_headers = _login(client, plain["username"], "Admin2-Pass-2026")
    target = _create_user(
        client, admin_headers, role_code="client", tenant_id=tenant_a["id"],
        password="Client2-Pass-2026",
    )

    denied = client.post(
        f"/api/admin/users/{target['id']}/permissions",
        headers=plain_headers,
        json={"permission_code": "audit.view", "effect": "allow", "reason": "试探"},
    )
    assert denied.status_code == 403
    assert "不可转授" in denied.json()["detail"]

    client.delete(f"/api/admin/users/{target['id']}", headers=admin_headers)
    client.delete(f"/api/admin/users/{plain['id']}", headers=admin_headers)


def test_user_level_permission_takes_effect_immediately(
    client: TestClient, admin_headers: dict[str, str], tenant_a: dict
) -> None:
    """授权后不重新登录就生效——权限每次请求都从数据库读。"""

    boss = _create_user(
        client, admin_headers, role_code="client", tenant_id=tenant_a["id"],
        password="Boss-Pass-2026",
    )
    boss_headers = _login(client, boss["username"], "Boss-Pass-2026")

    # 客户角色没有 audit.view
    assert client.get("/api/admin/audit-logs", headers=boss_headers).status_code == 403

    granted = client.post(
        f"/api/admin/users/{boss['id']}/permissions",
        headers=admin_headers,
        json={"permission_code": "answer.export", "effect": "allow", "reason": "需要导出"},
    )
    assert granted.status_code == 200
    me = client.get("/api/auth/me", headers=boss_headers).json()
    assert "answer.export" in me["user"]["permissions"]

    client.delete(f"/api/admin/users/{boss['id']}", headers=admin_headers)


def test_tenant_isolation_in_user_listing(
    client: TestClient, admin_headers: dict[str, str], tenant_a: dict, tenant_b: dict
) -> None:
    """按租户过滤：租户 A 的视图里不能出现租户 B 的用户。"""

    user_a = _create_user(client, admin_headers, role_code="client", tenant_id=tenant_a["id"])
    user_b = _create_user(client, admin_headers, role_code="client", tenant_id=tenant_b["id"])

    only_a = client.get(
        f"/api/admin/users?tenant_id={tenant_a['id']}", headers=admin_headers
    ).json()
    names = {item["username"] for item in only_a}
    assert user_a["username"] in names
    assert user_b["username"] not in names

    client.delete(f"/api/admin/users/{user_a['id']}", headers=admin_headers)
    client.delete(f"/api/admin/users/{user_b['id']}", headers=admin_headers)


def test_audit_log_records_grant_and_is_readonly(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.get("/api/admin/audit-logs?action=permission.granted", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert body["items"][0]["action"] == "permission.granted"
    # 审计接口只有 GET，没有任何写入或删除入口
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert (
            client.request(method, "/api/admin/audit-logs/1", headers=admin_headers).status_code
            in (404, 405)
        )


def test_change_password_flow(
    client: TestClient, admin_headers: dict[str, str], tenant_a: dict
) -> None:
    user = _create_user(
        client, admin_headers, role_code="client", tenant_id=tenant_a["id"],
        password="Old-Pass-2026",
    )
    user_headers = _login(client, user["username"], "Old-Pass-2026")

    wrong = client.post(
        "/api/auth/change-password",
        headers=user_headers,
        json={"old_password": "not-old", "new_password": "New-Pass-2026"},
    )
    assert wrong.status_code == 400

    ok = client.post(
        "/api/auth/change-password",
        headers=user_headers,
        json={"old_password": "Old-Pass-2026", "new_password": "New-Pass-2026"},
    )
    assert ok.status_code == 200
    assert _login(client, user["username"], "New-Pass-2026")
    assert (
        client.post(
            "/api/auth/login",
            json={"username": user["username"], "password": "Old-Pass-2026"},
        ).status_code
        == 401
    )

    client.delete(f"/api/admin/users/{user['id']}", headers=admin_headers)


def test_single_protected_admin_constraint_is_enforced() -> None:
    """数据库层面的硬约束：不能出现第二个固定管理员。"""

    engine = create_engine(_test_database_url())
    with Session(engine) as session:
        assert session.query(User).filter(User.is_protected.is_(True)).count() == 1
        session.add(
            User(
                username=f"second-{uuid.uuid4().hex[:6]}",
                email=f"second-{uuid.uuid4().hex[:6]}@example.com",
                display_name="第二个固定管理员",
                password_hash="pbkdf2_sha256$1000$x$y",
                is_protected=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    engine.dispose()


def test_health_still_ok_after_account_work(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200

"""健康检查接口测试。

这些测试不依赖真实数据库/向量库：健康检查接口在依赖不可用时
仍返回 200 并给出明细，因此测试在任何环境下结果都确定。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

EXPECTED_SERVICES = ("postgres", "qdrant", "redis", "embedding")


def test_health_returns_200_with_all_dependency_entries() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()

    assert body["status"] in {"ok", "degraded"}
    assert body["app"]
    assert body["version"]
    for name in EXPECTED_SERVICES:
        assert name in body["services"], f"缺少依赖项：{name}"
        assert body["services"][name]["status"] in {"ok", "down"}
        assert isinstance(body["services"][name]["latency_ms"], (int, float))


def test_health_status_is_consistent_with_failed_required_services() -> None:
    body = client.get("/api/health").json()

    if body["failed_required"]:
        assert body["status"] == "degraded"
    else:
        assert body["status"] == "ok"


def test_readiness_returns_503_when_required_dependency_is_down() -> None:
    health = client.get("/api/health").json()
    ready = client.get("/api/health/ready")

    if health["failed_required"]:
        assert ready.status_code == 503
    else:
        assert ready.status_code == 200


def test_root_redirects_to_api_docs() -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code in {307, 308}
    assert response.headers["location"] == "/docs"


def test_planning_feature_flag_is_disabled_by_default() -> None:
    """筹划功能在资质主体（F2）落实前必须保持关闭。"""

    assert client.get("/api/health").json()["planning_feature_enabled"] is False

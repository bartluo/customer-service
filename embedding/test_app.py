"""Embedding 服务测试（stub 后端，确定性可复现）。"""

import os

os.environ.setdefault("EMBEDDING_BACKEND", "stub")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402

client = TestClient(app)


def test_health_reports_backend_and_dimension() -> None:
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["backend"] == "stub"
    assert body["dense_dim"] == 1024


def test_embed_returns_dense_and_sparse_with_expected_shape() -> None:
    response = client.post("/embed", json={"texts": ["增值税一般计税方法", "小规模纳税人优惠"]})

    assert response.status_code == 200
    body = response.json()

    assert body["count"] == 2
    assert len(body["dense"]) == 2
    assert all(len(vector) == 1024 for vector in body["dense"])
    assert len(body["sparse"]) == 2
    assert all(len(vector["indices"]) > 0 for vector in body["sparse"])
    assert all(len(vector["indices"]) == len(vector["values"]) for vector in body["sparse"])


def test_embed_is_deterministic() -> None:
    payload = {"texts": ["同一个问题应当得到同一个向量"]}

    first = client.post("/embed", json=payload).json()
    second = client.post("/embed", json=payload).json()

    assert first["dense"] == second["dense"]
    assert first["sparse"] == second["sparse"]


def test_embed_rejects_empty_input() -> None:
    assert client.post("/embed", json={"texts": []}).status_code == 422

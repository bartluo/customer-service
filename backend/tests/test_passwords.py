"""密码哈希与令牌工具的单元测试。"""

from __future__ import annotations

import pytest

from app.security.passwords import (
    generate_password,
    hash_password,
    password_problems,
    verify_password,
)
from app.security.tokens import TokenError, create_access_token, decode_access_token


def test_hash_and_verify_roundtrip() -> None:
    encoded = hash_password("Str0ng-Passphrase!", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("Str0ng-Passphrase!", encoded)
    assert not verify_password("wrong-password", encoded)


def test_same_password_yields_different_hash() -> None:
    """同一密码两次哈希应不同（随机盐）；这是正确行为，不是缺陷。"""

    first = hash_password("Same-Password-1", iterations=1000)
    second = hash_password("Same-Password-1", iterations=1000)
    assert first != second
    assert verify_password("Same-Password-1", first)
    assert verify_password("Same-Password-1", second)


def test_malformed_hash_is_rejected_not_crashed() -> None:
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")
    assert not verify_password("", "pbkdf2_sha256$1000$abc$def")


def test_password_problems_gives_actionable_reasons() -> None:
    assert password_problems("Sh0rt") == ["长度至少 12 位"]
    assert password_problems("abcdefghijkl") == ["需包含数字"]
    assert password_problems("123456789012") == ["需包含字母"]
    assert password_problems("Str0ng-Passphrase!") == []


def test_generate_password_is_long_enough() -> None:
    assert len(generate_password(20)) >= 12
    with pytest.raises(ValueError):
        generate_password(8)


def test_token_roundtrip_carries_identity_not_permissions() -> None:
    token = create_access_token(user_id="u1", tenant_id="t1", is_protected=False)
    payload = decode_access_token(token)
    assert payload["sub"] == "u1"
    assert payload["tid"] == "t1"
    assert payload["protected"] is False
    # 权限不进令牌：授权变更必须立刻生效，不能等令牌过期
    assert "permissions" not in payload


def test_token_rejects_garbage() -> None:
    with pytest.raises(TokenError):
        decode_access_token("not-a-jwt")


def test_token_signed_with_other_secret_is_rejected() -> None:
    import jwt

    from app.config import get_settings

    settings = get_settings()
    forged = jwt.encode(
        {"sub": "u1", "type": "access", "protected": True},
        "a-different-secret",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_access_token(forged)

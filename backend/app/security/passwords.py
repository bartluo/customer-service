"""密码哈希与强度校验。

为什么用 PBKDF2-HMAC-SHA256 而不是 bcrypt/argon2：
  · 标准库自带，无需编译依赖，容器镜像更小，私有化部署更省事；
  · 财税系统涉及企业敏感数据，宁可用久经考验的标准库实现，也不想引入未审计的原生扩展。
迭代次数取 600000（OWASP 2023 建议值），单次哈希约 0.2～0.4 秒，是刻意选择的成本。

格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
"""

from __future__ import annotations
import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """生成密码哈希。同一密码每次结果不同（随机盐），这是正确行为。"""

    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "${}${}${}${}".format(
        ALGORITHM,
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ).lstrip("$")


def verify_password(password: str, encoded: str) -> bool:
    """校验密码。使用恒定时间比较，避免通过响应耗时侧信道猜密码。"""

    if not password or not encoded:
        return False
    try:
        algorithm, raw_iterations, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(raw_iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_password(length: int = 20) -> str:
    """生成随机初始口令（固定管理员首次启动时使用）。"""

    if length < 12:
        raise ValueError("初始口令长度不得少于 12 位")
    return secrets.token_urlsafe(length)[:length]


def password_problems(password: str, *, min_length: int = 12) -> list[str]:
    """返回口令不合规的原因列表；为空表示通过。用于给用户明确提示，而不是只说"太弱"。"""

    problems: list[str] = []
    if len(password) < min_length:
        problems.append(f"长度至少 {min_length} 位")
    if not any(char.isalpha() for char in password):
        problems.append("需包含字母")
    if not any(char.isdigit() for char in password):
        problems.append("需包含数字")
    return problems

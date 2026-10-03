"""对象存储自检：确认 S3 接口可用、密钥正确、越权被拒。

用法（项目根目录，需先 docker compose up -d）：
    python scripts/check_object_storage.py

为什么要有这个脚本：对象存储存的是法规原件、教材、题库这类"丢了就没法重解析"的原始文件。
密钥填错或权限被放开，往往要等到真正写文件时才发现。这里用最小的读写验证提前暴露问题。

说明：脚本自带 AWS SigV4 签名实现，不依赖 boto3，避免为了自检多装依赖。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 宿主机访问容器用的地址；容器内部署时由环境变量覆盖
DEFAULT_HOST_ENDPOINT = "http://localhost:9000"
DEFAULT_REGION = "us-east-1"
PROBE_KEY = "healthcheck/self-test.txt"


def _load_env_file(path: Path) -> dict[str, str]:
    """读取 .env，仅作为默认值；真实环境变量优先级更高。"""

    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class S3Client:
    """极简 S3 客户端：只够做自检。"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, region: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.host = self.endpoint.split("://", 1)[-1]
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region

    def request(self, method: str, path: str, body: bytes = b"") -> tuple[int, bytes]:
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        canonical_headers = (
            f"host:{self.host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, path, "", canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        key = _sign(f"AWS4{self.secret_key}".encode("utf-8"), datestamp)
        key = _sign(key, self.region)
        key = _sign(key, "s3")
        key = _sign(key, "aws4_request")
        signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            method=method,
            data=body or None,
            headers={
                "x-amz-date": amz_date,
                "x-amz-content-sha256": payload_hash,
                "Authorization": authorization,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


def _anonymous_status(endpoint: str) -> int:
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/", timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 - 自检脚本要展示任何异常
        print(f"  连接失败：{type(exc).__name__}: {exc}")
        return 0


def _report(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{'[OK]  ' if ok else '[FAIL]'} {label}{(' — ' + detail) if detail else ''}")
    return ok


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    from_file = _load_env_file(root / ".env")

    def setting(name: str, default: str = "") -> str:
        return os.environ.get(name) or from_file.get(name) or default

    endpoint = setting("S3_HOST_ENDPOINT", DEFAULT_HOST_ENDPOINT)
    region = setting("S3_REGION", DEFAULT_REGION)
    bucket = setting("S3_BUCKET", "customer-service")
    access_key = setting("S3_ACCESS_KEY")
    secret_key = setting("S3_SECRET_KEY")
    backup_key = setting("S3_BACKUP_ACCESS_KEY", "cs_s3_backup")
    backup_secret = setting("S3_BACKUP_SECRET_KEY", "cs_s3_backup_password")

    print("=" * 64)
    print("对象存储自检")
    print("=" * 64)
    print(f"地址：{endpoint}   存储桶：{bucket}")
    print("-" * 64)

    if not access_key or not secret_key:
        _report(False, "读取密钥", "未找到 S3_ACCESS_KEY / S3_SECRET_KEY（检查 .env）")
        return 1

    client = S3Client(endpoint, access_key, secret_key, region)
    results: list[bool] = []

    # 1. 匿名必须被拒
    status = _anonymous_status(endpoint)
    results.append(_report(status == 403, "匿名访问被拒绝", f"HTTP {status}（期望 403）"))

    # 2. 应用密钥：写 → 读 → 删
    payload = b"object-storage-self-test"
    status, _ = client.request("PUT", f"/{bucket}/{PROBE_KEY}", payload)
    results.append(_report(status == 200, "应用密钥写入", f"HTTP {status}"))

    status, body = client.request("GET", f"/{bucket}/{PROBE_KEY}")
    results.append(
        _report(status == 200 and body == payload, "应用密钥读回", f"HTTP {status}，内容一致={body == payload}")
    )

    status, _ = client.request("DELETE", f"/{bucket}/{PROBE_KEY}")
    results.append(_report(status in (200, 204), "应用密钥删除自检文件", f"HTTP {status}"))

    # 3. 越权必须被拒：应用密钥不能建新桶
    status, _ = client.request("PUT", "/self-test-should-be-denied")
    results.append(_report(status == 403, "应用密钥越权建桶被拒绝", f"HTTP {status}（期望 403）"))

    # 4. 备份账号只读
    if backup_key and backup_secret:
        backup = S3Client(endpoint, backup_key, backup_secret, region)
        status, _ = backup.request("GET", f"/{bucket}")
        results.append(_report(status == 200, "备份账号只读访问", f"HTTP {status}"))
        status, _ = backup.request("PUT", f"/{bucket}/{PROBE_KEY}", b"x")
        results.append(_report(status == 403, "备份账号写入被拒绝", f"HTTP {status}（期望 403）"))

    print("-" * 64)
    if all(results):
        print("结果：对象存储就绪 ✅")
        return 0

    print("结果：未通过")
    print()
    print("排查建议：")
    print("  1) 确认容器在跑：docker compose ps（cs_seaweedfs 应为 healthy）")
    print("  2) 密钥必须两处一致：.env 的 S3_ACCESS_KEY / S3_SECRET_KEY")
    print("     与 deploy/seaweedfs/s3.json 里的 accessKey / secretKey")
    print("  3) 改过配置后：docker compose up -d 让 SeaweedFS 重新读取")
    print(f"  4) 若桶不存在：docker compose run --rm s3_init（重建 {bucket}）")
    return 1


if __name__ == "__main__":
    sys.exit(main())

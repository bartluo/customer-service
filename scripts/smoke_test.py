"""环境自检脚本：检查各服务是否就绪，并给出可执行的修复建议。

用法（项目根目录）：
    python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

BACKEND_HEALTH = "http://localhost:8000/api/health"
QDRANT_HEALTH = "http://localhost:6333/readyz"
EMBEDDING_HEALTH = "http://localhost:8001/health"
TIMEOUT_SECONDS = 5


def _get(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return response.status < 400, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 自检脚本需要展示任何异常
        return False, f"{type(exc).__name__}: {exc}"


def _print(ok: bool, label: str, detail: str = "") -> None:
    mark = "[OK]  " if ok else "[FAIL]"
    print(f"{mark} {label}{(' — ' + detail) if detail else ''}")


def main() -> int:
    print("=" * 64)
    print("环境自检")
    print("=" * 64)

    failures: list[str] = []

    ok, body = _get(BACKEND_HEALTH)
    if not ok:
        _print(False, "后端 /api/health", body)
        failures.append("后端未启动")
    else:
        payload = json.loads(body)
        _print(True, "后端 /api/health", f"status={payload['status']} version={payload['version']}")
        for name, info in payload["services"].items():
            service_ok = info["status"] == "ok"
            _print(service_ok, f"  依赖 {name}", f"{info['status']} ({info['latency_ms']} ms)")
            if not service_ok:
                failures.append(f"依赖 {name} 不可用：{info.get('detail')}")

    ok, body = _get(QDRANT_HEALTH)
    _print(ok, "Qdrant 直连", "" if ok else body)
    if not ok:
        failures.append("Qdrant 直连失败")

    ok, body = _get(EMBEDDING_HEALTH)
    _print(ok, "Embedding 直连", body if ok else body)
    if not ok:
        failures.append("Embedding 服务不可用")

    print("-" * 64)
    if failures:
        print("结果：未就绪")
        for item in failures:
            print(f"  · {item}")
        print()
        print("建议：1) 确认 Docker Desktop 已启动；2) 执行 docker compose up -d；")
        print("      3) 等待 30 秒后重跑本脚本。")
        return 1

    print("结果：全部就绪 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

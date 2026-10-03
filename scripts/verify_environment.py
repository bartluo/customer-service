"""端到端验证脚本（不依赖 Docker）。

作用：在本机拉起 Embedding 服务与后端，检查健康接口与向量化接口是否可用，
验证完成后自动关闭这两个进程，不留残留。

用途：
  · 验收证据（G0）
  · 在没装 Docker 的机器上验证代码链路

用法（项目根目录）：
    python scripts/verify_environment.py

说明：容器里跑完整环境仍然用 `docker compose up -d`，本脚本只是"不依赖容器"的补充验证。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

ROOT = Path(__file__).resolve().parent.parent
EMBEDDING_DIR = ROOT / "embedding"
BACKEND_DIR = ROOT / "backend"

BACKEND_PORT = 18000
EMBEDDING_PORT = 18001

STARTUP_TIMEOUT_SECONDS = 40


def _hidden_process_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _get_json(url: str, timeout: float = 10.0) -> tuple[bool, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 400, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, {"http_status": exc.code}
    except Exception as exc:  # noqa: BLE001 - 验证脚本需要展示任何异常
        return False, f"{type(exc).__name__}: {exc}"


def _post_json(url: str, payload: dict) -> tuple[bool, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status < 400, json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _wait_until_ready(url: str, label: str) -> bool:
    deadline = time.time() + STARTUP_TIMEOUT_SECONDS
    while time.time() < deadline:
        ok, _ = _get_json(url, timeout=3)
        if ok:
            print(f"  [OK]   {label} 已就绪")
            return True
        time.sleep(1)
    print(f"  [FAIL] {label} 在 {STARTUP_TIMEOUT_SECONDS} 秒内未就绪")
    return False


def main() -> int:
    python = sys.executable
    flags = _hidden_process_flags()
    processes: list[subprocess.Popen] = []
    failures: list[str] = []

    print("=" * 68)
    print("端到端验证")
    print("=" * 68)

    print("\n[1/4] 启动 Embedding 服务（stub 后端）…")
    embedding_env = {
        **os.environ,
        "EMBEDDING_BACKEND": "stub",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    processes.append(
        subprocess.Popen(
            [
                python, "-m", "uvicorn", "app:app",
                "--host", "127.0.0.1", "--port", str(EMBEDDING_PORT),
                "--log-level", "warning",
            ],
            cwd=EMBEDDING_DIR,
            env=embedding_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    )
    embedding_ready = _wait_until_ready(
        f"http://127.0.0.1:{EMBEDDING_PORT}/health", "Embedding 服务"
    )
    if not embedding_ready:
        failures.append("Embedding 服务未启动")

    print("\n[2/4] 启动后端服务…")
    backend_env = {
        **os.environ,
        # 指向本机（此刻容器未启动，预期 postgres/qdrant/redis 显示 down，
        # 这正是"降级但不崩溃"行为的验证点）
        "DATABASE_URL": "postgresql+psycopg://cs_user:cs_password@127.0.0.1:5432/customer_service",
        "QDRANT_URL": "http://127.0.0.1:6333",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "EMBEDDING_URL": f"http://127.0.0.1:{EMBEDDING_PORT}",
        "HEALTH_CHECK_TIMEOUT_SECONDS": "1.0",
        "DB_CONNECT_TIMEOUT_SECONDS": "2",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    processes.append(
        subprocess.Popen(
            [
                python, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(BACKEND_PORT),
                "--log-level", "warning",
            ],
            cwd=BACKEND_DIR,
            env=backend_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    )
    backend_ready = _wait_until_ready(
        f"http://127.0.0.1:{BACKEND_PORT}/api/health", "后端服务"
    )
    if not backend_ready:
        failures.append("后端服务未启动")

    try:
        print("\n[3/4] 检查后端健康接口…")
        if backend_ready:
            ok, payload = _get_json(f"http://127.0.0.1:{BACKEND_PORT}/api/health", timeout=20)
            if ok and isinstance(payload, dict):
                print(f"  整体状态：{payload.get('status')}")
                print(f"  筹划开关：{payload.get('planning_feature_enabled')}（应为 False）")
                for name, info in payload.get("services", {}).items():  # type: ignore[union-attr]
                    mark = "OK  " if info["status"] == "ok" else "DOWN"
                    print(f"  [{mark}] {name:<10} {info['latency_ms']:>7.1f} ms")
            else:
                failures.append(f"健康接口异常：{payload}")

        print("\n[4/4] 检查向量化接口（稠密 + 稀疏）…")
        if embedding_ready:
            ok, payload = _post_json(
                f"http://127.0.0.1:{EMBEDDING_PORT}/embed",
                {"texts": ["增值税一般计税方法", "小规模纳税人月销售额未超过10万元"]},
            )
            if ok and isinstance(payload, dict):
                dense = payload["dense"]
                sparse = payload["sparse"]
                print(f"  后端：{payload['backend']}  模型：{payload['model']}")
                print(f"  稠密维度：{payload['dense_dim']}  条数：{payload['count']}")
                print(f"  第 1 条稠密向量前 3 位：{[round(v, 4) for v in dense[0][:3]]}")
                print(f"  第 1 条稀疏词项数：{len(sparse[0]['indices'])}")
                if payload["dense_dim"] != 1024:
                    failures.append(f"稠密维度应为 1024，实际 {payload['dense_dim']}")
                if not sparse[0]["indices"]:
                    failures.append("稀疏向量为空")
            else:
                failures.append(f"向量化接口异常：{payload}")
    finally:
        print("\n清理：关闭测试进程…")
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        print("  已全部关闭")

    print("\n" + "=" * 68)
    if failures:
        print("验证结果：未通过")
        for item in failures:
            print(f"  · {item}")
        return 1

    print("验证结果：代码链路通过 ✅")
    print("（postgres / qdrant / redis 显示 DOWN 属预期：容器尚未启动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

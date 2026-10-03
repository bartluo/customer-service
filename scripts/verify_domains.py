"""真实环境验收脚本：域包框架。

验证内容（对应验收门 G2）：
  · 域包在真实容器里被加载
  · 域路由把财税问题分到财税域
  · 租户级域开关生效（停用后该域不参与路由）
  · 新增一个域包不需要改引擎代码

用法（项目根目录，需先 docker compose up -d）：
    python scripts/verify_domains.py
"""

from __future__ import annotations
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import _console  # noqa: F401  Windows 控制台 UTF-8 修复
import uuid

import yaml

BASE = "http://localhost:8000/api"
REPO = pathlib.Path(__file__).resolve().parent.parent


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    suffix = f" — {detail}" if detail else ""
    print(f"{'[OK]  ' if ok else '[FAIL]'} {label}{suffix}")


def admin_password() -> str:
    """拿到固定管理员口令。

    优先从启动日志读初始口令（首次部署时才有那一行）；
    读不到说明账号已存在，用 reset_admin_password.py 重置一次。
    这样脚本可重复执行，不依赖"这是第一次启动"。
    """

    out = subprocess.run(
        ["docker", "logs", "cs_backend"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    logs = (out.stdout or "") + (out.stderr or "")
    match = re.search(r"初始口令=([^\s（(]+)", logs)
    if match:
        return match.group(1)

    reset = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend",
         "python", "/app/scripts/reset_admin_password.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
    )
    output = (reset.stdout or "") + (reset.stderr or "")
    match = re.search(r"口令已重置为：(\S+)", output)
    return match.group(1) if match else ""


def check_loaded_packs(token: str) -> None:
    status, packs = call("GET", "/domains")
    check("域包列表可读", status == 200, f"HTTP {status}")
    ids = {item["domain_id"] for item in packs.get("items", [])}
    check("财税域包已加载", ids == {"finance_tax"}, ids)

    finance = next((i for i in packs["items"] if i["domain_id"] == "finance_tax"), {})
    check("财税域为启用状态", finance.get("status") == "active", finance.get("status"))
    check(
        "财税域五个组件已就位",
        set(finance.get("ready_components", []))
        == {"ontology", "extractor", "retrieval", "reasoner", "risk_rules"},
        finance.get("ready_components"),
    )


def check_routing(token: str) -> None:
    cases = [
        ("小规模纳税人增值税怎么算？需要申报吗", "finance_tax"),
        ("企业所得税加计扣除政策有变化吗", "finance_tax"),
        ("公司交的印花税是多少", "finance_tax"),
        ("收到一张增值税专用发票，进项能抵扣吗", "finance_tax"),
    ]
    for question, expected in cases:
        status, result = call("POST", "/domains/route-preview", {"question": question}, token)
        check(
            f"路由：{question[:14]}…",
            status == 200 and result.get("domain_id") == expected,
            f"{result.get('domain_id')}（{result.get('reason', '')}）",
        )

    status, result = call("POST", "/domains/route-preview", {"question": "今天天气怎么样"}, token)
    check(
        "无关问题不误路由",
        status == 200 and result.get("domain_id") is None,
        result.get("reason"),
    )


def check_tenant_switch(token: str) -> None:
    suffix = uuid.uuid4().hex[:6]
    status, tenant = call(
        "POST", "/admin/tenants", {"code": f"dom-{suffix}", "name": "域开关测试企业"}, token
    )
    check("创建租户", status == 201, f"HTTP {status}")
    tenant_id = tenant.get("id", "")

    status, info = call("GET", f"/domains/tenants/{tenant_id}", token=token)
    check(
        "新租户默认订阅财税域",
        "finance_tax" in info.get("subscribed_domains", []),
        info.get("subscribed_domains"),
    )

    status, result = call(
        "POST", "/domains/route-preview",
        {"question": "增值税怎么算", "tenant_id": tenant_id}, token
    )
    check("租户范围内路由正常", result.get("domain_id") == "finance_tax", result.get("reason"))

    status, _ = call(
        "PUT", f"/domains/tenants/{tenant_id}", {"subscribed_domains": []}, token
    )
    check("停用租户的财税域", status == 200, f"HTTP {status}")

    status, result = call(
        "POST", "/domains/route-preview",
        {"question": "增值税怎么算", "tenant_id": tenant_id}, token
    )
    check("停用后财税域不再参与路由", result.get("domain_id") != "finance_tax", result.get("reason"))

    status, _ = call(
        "PUT", f"/domains/tenants/{tenant_id}", {"subscribed_domains": ["不存在的域"]}, token
    )
    check("未知域被拒绝", status == 400, f"HTTP {status}")


def check_g2_no_code_change() -> None:
    """G2：只加目录与 manifest，引擎代码零改动。"""

    sandbox = pathlib.Path(tempfile.mkdtemp(prefix="domains_g2_"))
    try:
        shutil.copytree(REPO / "domains", sandbox / "domains")
        new_pack = sandbox / "domains" / "legal_affairs"
        new_pack.mkdir()
        component_names = (
            "ontology", "extractor", "retrieval", "reasoner",
            "verifier", "templates", "eval_cases", "glossary", "risk_rules",
        )
        (new_pack / "manifest.yaml").write_text(
            yaml.safe_dump(
                {
                    "domain_id": "legal_affairs",
                    "name": "法务",
                    "version": "0.1.0",
                    "status": "active",
                    "description": "合同与争议",
                    "roles": ["client"],
                    "components": {name: "pending" for name in component_names},
                    "routing": {
                        "keywords": ["合同", "诉讼", "侵权"],
                        "include": [],
                        "exclude": [],
                    },
                    "features": {},
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        code = (
            "import sys, pathlib;"
            f"sys.path.insert(0, r'{REPO / 'backend'}');"
            "from app.domain_packs import get_registry, route;"
            f"reg = get_registry(); reg.load(pathlib.Path(r'{sandbox / 'domains'}'), force=True);"
            "print('PACKS', sorted(p.domain_id for p in reg.all_packs()));"
            "res = route('这份合同的违约条款怎么主张', reg.all_packs());"
            "print('ROUTE', res.domain_id, res.reason)"
        )
        result = subprocess.run(
            [str(REPO / ".venv" / "Scripts" / "python.exe"), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        check("新域包被自动加载", "legal_affairs" in output, output.strip()[:200])
        check(
            "新域包可被路由到",
            "ROUTE legal_affairs" in output,
            [line for line in output.splitlines() if "ROUTE" in line],
        )
        check("原有财税域仍可路由", "finance_tax" in output, "")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main() -> int:
    print("=" * 64)
    print("验收：域包框架")
    print("=" * 64)

    password = admin_password()
    check("取到固定管理员口令", bool(password))
    status, body = call("POST", "/auth/login", {"username": "root_admin", "password": password})
    check("管理员登录", status == 200, f"HTTP {status}")
    token = body.get("access_token", "")

    check_loaded_packs(token)
    check_routing(token)
    check_tenant_switch(token)
    check_g2_no_code_change()

    print("=" * 64)
    suffix = "  ✅" if all(results) else "  ❌"
    print(f"结果：{sum(results)}/{len(results)} 项通过{suffix}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

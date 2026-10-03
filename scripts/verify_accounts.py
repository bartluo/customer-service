"""真实环境验收脚本：对运行中的后端跑一遍完整业务流程。

可重复执行：每次用随机后缀创建租户与账号，不与既有数据冲突。
用法（项目根目录，需先 docker compose up -d）：
    python scripts/verify_accounts.py
"""

import json
import urllib.error
import urllib.request
import uuid

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

BASE = "http://localhost:8000/api"


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"{'[OK]  ' if ok else '[FAIL]'} {label}{(' — ' + str(detail)) if detail else ''}")


import subprocess, re

logs = subprocess.run(
    ["docker", "logs", "cs_backend"], capture_output=True, text=True, encoding="utf-8", errors="replace"
).stdout + subprocess.run(
    ["docker", "logs", "cs_backend"], capture_output=True, text=True, encoding="utf-8", errors="replace"
).stderr
match = re.search(r"初始口令=([^\s（(]+)", logs)
password = match.group(1) if match else ""
if not password:
    # 账号已存在（不是首次启动），启动日志里不会再打初始口令。
    # 用重置脚本拿一个当前可用的口令，让本脚本可重复执行。
    import pathlib

    repo = pathlib.Path(__file__).resolve().parent.parent
    reset = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend",
         "python", "/app/scripts/reset_admin_password.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(repo),
    )
    reset_output = (reset.stdout or "") + (reset.stderr or "")
    found = re.search(r"口令已重置为：(\S+)", reset_output)
    password = found.group(1) if found else ""
check("取得固定管理员口令（首次部署取初始口令，重跑则重置）", bool(password), password)

status, body = call("POST", "/auth/login", {"username": "root_admin", "password": password})
check("固定管理员登录", status == 200, f"HTTP {status}")
token = body.get("access_token", "")
check("固定管理员不走角色授权（直接全权）",
      body.get("user", {}).get("is_protected") and not body.get("user", {}).get("permissions"),
      f"roles={body.get('user', {}).get('roles')} permissions={body.get('user', {}).get('permissions')}")

status, me = call("GET", "/auth/me", token=token)
check("查看身份", status == 200 and me["user"]["is_protected"], f"HTTP {status}")

status, perms = call("GET", "/admin/permissions", token=token)
check("权限清单可读", status == 200 and len(perms) == 20, f"HTTP {status}，共 {len(perms) if status==200 else 0} 项")

status, roles = call("GET", "/admin/roles", token=token)
codes = {r["code"] for r in roles} if status == 200 else set()
check("三个内置角色", codes == {"admin", "reviewer", "client"}, codes)

suffix = uuid.uuid4().hex[:6]
status, tenant = call("POST", "/admin/tenants",
                      {"code": f"demo-{suffix}", "name": "示范企业"}, token)
check("创建租户", status == 201, f"HTTP {status}")
tenant_id = tenant.get("id", "")

status, created = call(
    "POST", "/admin/users",
    {"username": f"boss{suffix}", "email": f"boss{suffix}@demo.example.com", "display_name": "张总",
     "password": "Boss-Pass-2026", "role_code": "client", "tenant_id": tenant_id},
    token,
)
check("创建客户账号", status == 201, f"HTTP {status}")
boss_id = created.get("user", {}).get("id", "")

status, b = call("POST", "/auth/login", {"username": f"boss{suffix}", "password": "Boss-Pass-2026"})
check("客户账号登录", status == 200, f"HTTP {status}")
boss_token = b.get("access_token", "")

status, _ = call("GET", "/admin/users", token=boss_token)
check("客户访问管理接口被拒(403)", status == 403, f"HTTP {status}")

status, _ = call("GET", "/admin/tenants", token=None)
check("未登录访问被拒(401)", status == 401, f"HTTP {status}")

status, granted = call(
    "POST", f"/admin/users/{boss_id}/permissions",
    {"permission_code": "answer.export", "effect": "allow", "reason": "需要导出底稿"},
    token,
)
check("给客户单独授权 answer.export", status == 200 and granted["applied"], f"HTTP {status}")

status, me2 = call("GET", "/auth/me", token=boss_token)
check("授权立刻生效(无需重新登录)", "answer.export" in me2["user"]["permissions"],
      me2["user"]["permissions"])

status, res = call(
    "POST", f"/admin/users/{boss_id}/permissions",
    {"permission_code": "answer.export", "effect": "deny", "reason": "临时收回"},
    token,
)
status, me3 = call("GET", "/auth/me", token=boss_token)
check("收权也立刻生效", "answer.export" not in me3["user"]["permissions"], f"HTTP {status}")

status, reviewer = call(
    "POST", "/admin/users",
    {"username": f"expert{suffix}", "email": f"expert{suffix}@demo.example.com", "display_name": "李专家",
     "password": "Expert-Pass-2026", "role_code": "reviewer"},
    token,
)
check("固定管理员可创建审核专家", status == 201, f"HTTP {status}")
expert_me = call("GET", "/auth/me", token=call("POST", "/auth/login",
                 {"username": f"expert{suffix}", "password": "Expert-Pass-2026"})[1]["access_token"])[1]
check("审核专家含 knowledge.review", "knowledge.review" in expert_me["user"]["permissions"])

status, plain = call(
    "POST", "/admin/users",
    {"username": f"admin{suffix}", "email": f"admin{suffix}@demo.example.com", "display_name": "王管理",
     "password": "Admin-Pass-2026", "role_code": "admin"},
    token,
)
plain_token = call("POST", "/auth/login", {"username": f"admin{suffix}", "password": "Admin-Pass-2026"})[1]["access_token"]
status, denied = call("POST", f"/admin/users/{boss_id}/roles", {"role_code": "reviewer"}, plain_token)
check("普通管理员授审核专家被拒(403)", status == 403, f"HTTP {status} {denied.get('detail','')}")

status, denied2 = call("POST", f"/admin/users/{boss_id}/permissions",
                       {"permission_code": "audit.view", "effect": "allow"}, plain_token)
check("普通管理员转授不可转授权限被拒(403)", status == 403, f"HTTP {status}")

me_id = call("GET", "/auth/me", token=token)[1]["user"]["id"]
status, _ = call("DELETE", f"/admin/users/{me_id}", token=token)
check("固定管理员不可删除(403)", status == 403, f"HTTP {status}")
status, _ = call("PATCH", f"/admin/users/{me_id}", {"is_active": False}, token)
check("固定管理员不可停用(403)", status == 403, f"HTTP {status}")

status, audit = call("GET", "/admin/audit-logs?limit=200", token=token)
actions = {a["action"] for a in audit["items"]}
check("审计日志已留痕", status == 200 and {"user.created", "permission.granted"} <= actions,
      f"共 {audit['total']} 条")

print("=" * 64)
print(f"结果：{sum(results)}/{len(results)} 项通过" + ("  ✅" if all(results) else "  ❌"))

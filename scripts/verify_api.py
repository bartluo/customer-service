"""前端与后台验收。

跑法（项目根目录，需先 docker compose up -d，且前端已构建）：
    python scripts/verify_api.py

检查项：
  · G10-A 对外接口：问答六段式、引用可点开、计算分步、合规展示、留痕、计算器、接口清单、权限拦截
  · G10-B 筹划与复核台：画像 schema、四道合法性检查、**开关关闭时不对客户输出方案**、复核队列
  · G10-C 管理后台与提醒：概览、缺口、评测与门禁、政策变化（含"未配置推送"的诚实说明）
  · G10-D 前端：工程文件齐全、页面能打开并渲染出内容

退出码：0 = 全部通过；1 = 有失败项

关于口令：固定管理员口令只在首次启动时打印。
本脚本按"环境变量 → 上次用过的口令文件 → 启动日志 → 重置"的顺序取，
**取到的口令会写进 .admin_password.local（已在 .gitignore）**。
为什么不像 verify_accounts 那样每次都重置：每跑一次验收就把已经登录的
页面全部踢下线，这个代价比"本地存一个口令"大得多（同一台机器上 .env 里
本来就放着数据库口令与 JWT 密钥）。

关于 G10-B 的"客户看不见方案"：
  这是定下的红线（ADR-0015）在接口层的落地。脚本会真的建一个
  **客户角色**的账号去打这个接口——用固定管理员测是测不出来的，
  固定管理员对所有权限都放行。
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
BACKEND = "http://localhost:8000/api"
FRONTEND = "http://localhost:3000"
VERIFY_TENANT_CODE = "verify-phase10"
PASSWORD_FILE = REPO / ".admin_password.local"


def call(method: str, path: str, body=None, token: str | None = None, base: str = BACKEND):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001 - 错误响应不是 JSON 时也不能让脚本崩
            return exc.code, {}
    except Exception as exc:  # noqa: BLE001 - 连不上后端时给出可读信息
        return 0, {"detail": f"{type(exc).__name__}: {exc}"}


class Checker:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.start = time.time()

    def check(self, code: str, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(f"{code} {name}")
            print(f"  [通过] {code} {name}")
        else:
            self.failed.append((f"{code} {name}", detail))
            print(f"  [失败] {code} {name} — {detail}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print()
        print(f"验收结果：{len(self.passed)}/{total} 通过，用时 {time.time() - self.start:.1f} 秒")
        if self.failed:
            print("失败清单：")
            for name, detail in self.failed:
                print(f"  - {name}：{detail}")
            return 1
        return 0


def _works(password: str) -> bool:
    status, body = call("POST", "/auth/login", {"username": "root_admin", "password": password})
    return status == 200 and bool(body.get("access_token"))


def _admin_password(verbose) -> str:
    """取一个当前可用的固定管理员口令。优先复用，实在没有才重置。"""

    import os

    candidates: list[tuple[str, str]] = []
    from_env = os.environ.get("ADMIN_PASSWORD", "").strip()
    if from_env:
        candidates.append(("环境变量 ADMIN_PASSWORD", from_env))
    if PASSWORD_FILE.exists():
        candidates.append(("上次用过的口令文件", PASSWORD_FILE.read_text(encoding="utf-8").strip()))

    for source, candidate in candidates:
        if candidate and _works(candidate):
            verbose(f"沿用现有口令（{source}）")
            return candidate

    logs = subprocess.run(
        ["docker", "logs", "cs_backend"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"(?:初始口令|口令已重置为)[=：]([^\s（(]+)", (logs.stdout or "") + (logs.stderr or ""))
    if match and _works(match.group(1)):
        verbose("沿用启动日志里的口令")
        PASSWORD_FILE.write_text(match.group(1), encoding="utf-8")
        return match.group(1)

    reset = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "backend",
            "python", "/app/scripts/reset_admin_password.py",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
    )
    found = re.search(r"口令已重置为：(\S+)", (reset.stdout or "") + (reset.stderr or ""))
    if found:
        PASSWORD_FILE.write_text(found.group(1), encoding="utf-8")
        verbose("口令已重置（原口令不可用），已记到 .admin_password.local")
        return found.group(1)
    return ""


def _ensure_tenant(token: str) -> str:
    """拿一个干净的客户账号专用租户。已存在就复用，避免每跑一次多一堆租户。"""

    status, tenants = call("GET", "/admin/tenants", token=token)
    if status == 200:
        for item in tenants:
            if item.get("code") == VERIFY_TENANT_CODE:
                return item["id"]
    status, created = call(
        "POST",
        "/admin/tenants",
        {"code": VERIFY_TENANT_CODE, "name": "验收租户"},
        token=token,
    )
    return created.get("id", "") if status in (200, 201) else ""


def fetch_text(url: str) -> tuple[int, str]:
    """取一段纯文本（前端页面是 HTML，不是 JSON，不能用 call）。"""

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _create_client(token: str, tenant_id: str, checker: Checker) -> dict:
    suffix = uuid.uuid4().hex[:6]
    password = "Client-Pass-2026"
    username = f"v10{suffix}"
    status, body = call(
        "POST",
        "/admin/users",
        {
            "username": username,
            "email": f"{username}@example.com",
            "display_name": "验收客户",
            "password": password,
            "role_code": "client",
            "tenant_id": tenant_id,
        },
        token=token,
    )
    if status != 201:
        checker.check("B0", "创建客户角色账号（用于验证不对客户输出方案）", False, str(body))
        return {}
    login_status, login = call("POST", "/auth/login", {"username": username, "password": password})
    if login_status != 200:
        checker.check("B0", "客户账号登录", False, str(login))
        return {}
    checker.check("B0", "创建并登录客户角色账号", True)
    return {"id": body["user"]["id"], "token": login["access_token"], "username": username}


def main() -> int:
    checker = Checker()

    print("[准备] 登录")
    password = _admin_password(lambda text: print(f"      {text}"))
    status, body = call("POST", "/auth/login", {"username": "root_admin", "password": password})
    checker.check("P1", "固定管理员登录", status == 200 and bool(body.get("access_token")), f"HTTP {status}")
    token = body.get("access_token", "")
    if not token:
        return checker.summary()
    print(f"      当前管理员口令：{password}（已存 .admin_password.local，前端登录用它）")

    print("\n[G10-A] 对外接口")
    status, ask = call(
        "POST",
        "/v1/qa/ask",
        {"question": "我是小规模纳税人，上个月在市区卖了10万元含税货物，怎么交税"},
        token=token,
    )
    ok = status == 200 and isinstance(ask.get("answer"), dict)
    checker.check("A1", "问答返回结构化六段式答案", ok, f"HTTP {status} {ask.get('detail', '')}")
    if ok:
        sections = ask["answer"].get("sections", [])
        keys = [item.get("key") for item in sections]
        renderers = {item.get("key"): item.get("renderer") for item in sections}
        checker.check(
            "A2",
            "必填段齐全，且各段带渲染方式（renderer）",
            {"judgement", "basis", "disclaimer"}.issubset(set(keys))
            and renderers.get("disclaimer") == "fixed_text"
            and renderers.get("basis") == "citation_list",
            f"段落={keys}",
        )
        calc_section = next((item for item in sections if item.get("key") == "calculation"), None)
        steps = (calc_section or {}).get("content") or []
        checker.check(
            "A3",
            "计算过程分步（公式 + 代入 + 结果 + 依据）",
            bool(steps) and all(item.get("formula") and item.get("result") for item in steps),
            f"{len(steps)} 步" if steps else "无计算段",
        )
        citations = ask.get("citations") or []
        checker.check(
            "A4",
            "引用带条款定位与效力状态（可点开）",
            bool(citations)
            and all(item.get("article_version_id") for item in citations)
            and all(item.get("effect_status") for item in citations),
            f"{len(citations)} 条引用",
        )
        # 「情形判定」必须是答案而不是文件清单（ADR-0020）：
        # 试用时的原话是"现在直接给了（适）用文件…应该是你给我答案，
        # 完了备注说明依据哪些文件"。
        judgement = next(
            (item.get("content") for item in sections if item.get("key") == "judgement"), ""
        )
        judgement_text = judgement if isinstance(judgement, str) else ""
        checker.check(
            "A11",
            "判定段给的是答案（含条文要点 + 出处备注），不是文件清单",
            (
                "（出处：" in judgement_text
                or "本次计算依据的政策" in judgement_text
                or "结论：" in judgement_text
            )
            and "可以适用：" not in judgement_text,
            judgement_text[:60].replace("\n", " "),
        )
        compliance = ask.get("compliance") or {}
        checker.check(
            "A5",
            "合规展示：免责声明 + 知识截至时间 + 留痕编号",
            bool(compliance.get("disclaimer"))
            and bool(compliance.get("knowledge_as_of"))
            and bool(compliance.get("trace_id")),
            f"trace={compliance.get('trace_id')}",
        )

        if citations:
            detail_status, detail = call(
                "GET",
                f"/v1/citations/{citations[0]['article_version_id']}",
                token=token,
            )
            checker.check(
                "A6",
                "引用详情能查到原文与生效区间",
                detail_status == 200 and bool(detail.get("content")) and "repealed" in detail,
                f"HTTP {detail_status}",
            )

        trace_id = compliance.get("trace_id")
        log_status, logs = call(
            "GET", f"/v1/console/audit-logs?action=qa.ask&limit=50", token=token
        )
        found = log_status == 200 and any(
            item.get("target_id") == trace_id for item in logs.get("items", [])
        )
        checker.check("A7", "这次问答已留痕（审计日志可查到）", found, f"trace={trace_id}")

    # 超过免征标准（月销售额 10 万元）→ 走"3% 减按 1%"，能算出数
    status, calc = call(
        "POST",
        "/v1/calc/tax",
        {
            "taxpayer_type": "小规模纳税人",
            "sales_amount": "300000",
            "amount_includes_tax": True,
            "location": "市区",
            # 期间口径必须给：含税 30 万按月看是应税（>10万）、按季看是免征（≤30万），
            # 不给期间系统两档都算，结论不一致就不出数（这是有意的）。
            "period_scope": "month",
        },
        token=token,
    )
    checker.check(
        "A8",
        "独立计算器可用（增值税 + 附加税费，含分步）",
        status == 200
        and calc.get("vat", {}).get("computable")
        and calc.get("surcharges") is not None
        and float(calc.get("total_payable", 0)) > 0,
        f"HTTP {status} 合计={calc.get('total_payable')}",
    )

    # 未超过免征标准 → 必须先套优惠政策再算，答案是免征（0 元），并带上依据。
    # 回归背景：用户问"小规模纳税人卖 10 万含税货物怎么交税"，
    # 若照 3% 硬算会得到 2,912.62 元，而正确答案是免征。
    status, exempt = call(
        "POST",
        "/v1/calc/tax",
        {
            "taxpayer_type": "小规模纳税人",
            "sales_amount": "100000",
            "amount_includes_tax": True,
            "location": "市区",
            "period_scope": "month",
        },
        token=token,
    )
    vat_block = exempt.get("vat") or {}
    steps = vat_block.get("steps") or []
    checker.check(
        "A12",
        "先查政策再算税：未超免征标准时给出免征并标注依据",
        status == 200
        and str(vat_block.get("payable")) in {"0.00", "0.0", "0"}
        and any("免征" in str(step.get("title", "")) for step in steps)
        and any("2023年第19号" in str(step.get("citation", "")) for step in steps),
        f"HTTP {status} 应纳={vat_block.get('payable')} 步数={len(steps)}",
    )

    # 期间口径决定用哪一档免征标准：同一笔含税 25 万，
    # 按季（30万标准）免征、按月（10万标准）应税。
    quarter = call(
        "POST",
        "/v1/calc/tax",
        {
            "taxpayer_type": "小规模纳税人",
            "sales_amount": "250000",
            "amount_includes_tax": True,
            "period_scope": "quarter",
        },
        token=token,
    )[1]
    month = call(
        "POST",
        "/v1/calc/tax",
        {
            "taxpayer_type": "小规模纳税人",
            "sales_amount": "250000",
            "amount_includes_tax": True,
            "period_scope": "month",
        },
        token=token,
    )[1]
    checker.check(
        "A13",
        "免税标准跟问题里的期间走（按季 30 万 / 按月 10 万）",
        str((quarter.get("vat") or {}).get("payable")) in {"0.00", "0.0", "0"}
        and float((month.get("vat") or {}).get("payable") or 0) > 0,
        f"按季={((quarter.get('vat') or {}).get('payable'))} 按月={((month.get('vat') or {}).get('payable'))}",
    )

    status, spec = call("GET", "/openapi.json", base="http://localhost:8000")
    required_paths = {
        "/api/v1/qa/ask",
        "/api/v1/calc/tax",
        "/api/v1/planning/analyze",
        "/api/v1/planning/reviews",
        "/api/v1/citations/{article_version_id}",
    }
    available = set((spec.get("paths") or {}).keys()) if status == 200 else set()
    checker.check(
        "A9",
        "对外接口清单齐全（技术方案 12.4）",
        required_paths.issubset(available),
        f"缺少 {sorted(required_paths - available)}" if status == 200 else f"HTTP {status}",
    )

    status, _ = call("GET", "/v1/console/overview")
    checker.check("A10", "未登录访问受保护接口被拒（401）", status == 401, f"HTTP {status}")

    print("\n[G10-B] 筹划与复核台")
    status, schema = call("GET", "/v1/planning/profile-schema", token=token)
    checker.check(
        "B1",
        "画像字段与允许取值可读（前端表单不用抄枚举）",
        status == 200 and bool(schema.get("enums", {}).get("entity_type")),
        f"HTTP {status}",
    )

    profile = {
        "entity_type": "有限公司",
        "taxpayer_type": "小规模纳税人",
        "industry": "软件和信息技术服务业",
        "region": "市区",
        "employees": 20,
        "annual_revenue": "900000",
        "revenue": "900000",
        "cost": "600000",
        "profit": "300000",
        "taxable_income": "300000",
        "total_assets": "2000000",
        "goal": "降低税负",
        "goal_detail": "希望在不改变业务的前提下降低综合税负",
        "risk_preference": "稳健",
        "flexible": {"can_change_contract": True, "can_change_entity": False, "can_change_timing": True},
        "business_purpose": "为客户提供持续的软件运维服务，客户要求按月结算",
        "business_benefit": "按月结算降低客户资金压力，提高了续约率",
        "evidence_available": ["服务合同", "履约记录"],
        "business_authentic": True,
    }
    status, analysis = call(
        "POST",
        "/v1/planning/analyze",
        {"profile": profile, "request_text": "希望在不改变业务的前提下降低综合税负"},
        token=token,
    )
    checks = (analysis.get("legality") or {}).get("checks") or []
    checker.check(
        "B2",
        "筹划分析返回四道合法性检查结论",
        status == 200 and len(checks) == 4,
        f"HTTP {status} 检查项={[item.get('name') for item in checks]}",
    )
    checker.check(
        "B3",
        "筹划总开关状态如实返回",
        status == 200 and "planning_enabled" in analysis,
        f"planning_enabled={analysis.get('planning_enabled')}",
    )

    tenant_id = _ensure_tenant(token)
    client = _create_client(token, tenant_id, checker) if tenant_id else {}
    if client:
        status, client_view = call(
            "POST",
            "/v1/planning/analyze",
            {"profile": profile, "request_text": "能不能通过拆分合同少交税"},
            token=client["token"],
        )
        no_plans = status == 200 and client_view.get("plans") == []
        checker.check(
            "B4",
            "开关关闭时不给客户输出任何方案（ADR-0015 红线）",
            no_plans and bool(client_view.get("notes")),
            f"HTTP {status} 方案数={len(client_view.get('plans') or [])}",
        )
        status, _ = call("GET", "/v1/console/overview", token=client["token"])
        checker.check("B5", "客户角色访问管理后台被拒（403）", status == 403, f"HTTP {status}")
        call("DELETE", f"/admin/users/{client['id']}", token=token)

    status, reviews = call("GET", "/v1/planning/reviews", token=token)
    workload_status, workload = call("GET", "/v1/planning/reviews/workload", token=token)
    checker.check(
        "B6",
        "复核队列与工作量可读",
        status == 200 and workload_status == 200 and "pending" in workload,
        f"待办 {workload.get('pending')} 条",
    )

    print("\n[G10-C] 管理后台与政策提醒")
    status, overview = call("GET", "/v1/console/overview", token=token)
    checker.check(
        "C1",
        "概览：法规数 / 条文数 / 索引点 / 缺口",
        status == 200
        and overview.get("regulations_total", 0) > 0
        and overview.get("index_points") is not None
        and "open_gaps" in overview,
        f"HTTP {status} 索引点={overview.get('index_points')} degraded={overview.get('degraded')}",
    )
    status, gaps = call("GET", "/v1/console/gaps", token=token)
    checker.check("C2", "缺口清单可读", status == 200 and "total" in gaps, f"HTTP {status}")
    status, evaluations = call("GET", "/v1/console/evaluations", token=token)
    checker.check(
        "C3",
        "评测报告与门禁结论可读",
        status == 200 and "runs" in evaluations and "decisions" in evaluations,
        f"HTTP {status} 评测 {len(evaluations.get('runs') or [])} 次",
    )
    status, changes = call("GET", "/v1/console/policy-changes", token=token)
    delivery = (changes.get("digest") or {}).get("delivery", "")
    checker.check(
        "C4",
        "政策变化摘要可用，且未配置推送时如实说明",
        status == 200 and bool(changes.get("digest")) and "未配置" in str(delivery),
        f"HTTP {status} 推送状态={delivery}",
    )

    print("\n[G10-D] 前端")
    required_files = [
        "frontend/package.json",
        "frontend/next.config.mjs",
        "frontend/src/app/layout.tsx",
        "frontend/src/app/page.tsx",
        "frontend/src/app/login/page.tsx",
        "frontend/src/app/calculator/page.tsx",
        "frontend/src/app/planning/page.tsx",
        "frontend/src/app/review/page.tsx",
        "frontend/src/app/admin/page.tsx",
        "frontend/src/app/policy-changes/page.tsx",
        "frontend/src/components/answer/AnswerView.tsx",
        "frontend/src/components/answer/CitationCard.tsx",
        "frontend/src/lib/api.ts",
    ]
    missing = [item for item in required_files if not (REPO / item).exists()]
    checker.check("D1", "前端工程文件齐全（9 个页面 + 组件 + 接口层）", not missing, f"缺少 {missing}")

    status, html = fetch_text(f"{FRONTEND}/login")
    checker.check(
        "D2",
        "前端页面能打开",
        status == 200,
        f"HTTP {status}（若连接失败：先安装前端依赖并 docker compose up -d frontend）",
    )
    checker.check(
        "D3",
        "页面渲染出真实内容（不是空白骨架）",
        status == 200 and "财税知识引擎" in html,
        "登录页未渲染出标题" if status == 200 else "页面没打开",
    )

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())

"""真实环境验收脚本：财税知识结构化。

验证内容（对应验收门 G3）：
  · 知识库接口已挂载，权限正确
  · 批量导入：进度、失败清单、待复核清单
  · 条文层级切分正确（章/节/条/款/项）
  · 元数据识别：文号、发文机关、施行日期
  · 时间区间无重叠
  · 效力状态过滤规则生效

用法（项目根目录，需先 docker compose up -d）：
    python scripts/verify_knowledge.py
"""

from __future__ import annotations
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

BASE = "http://localhost:8000/api"
REPO = pathlib.Path(__file__).resolve().parent.parent

results: list[bool] = []


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # URL 里的非 ASCII 字符要先百分号编码，否则 http.client 会按 ascii 编码报错
    safe_path = urllib.parse.quote(path, safe="/?=&%")
    request = urllib.request.Request(BASE + safe_path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"detail": raw.decode("utf-8", errors="replace")}


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    suffix = f" — {detail}" if detail else ""
    print(f"{'[OK]  ' if ok else '[FAIL]'} {label}{suffix}")


def _candidate_passwords() -> list[str]:
    env_file = REPO / ".env"
    passwords = []
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            if raw.strip().startswith("BOOTSTRAP_ADMIN_PASSWORD="):
                passwords.append(raw.split("=", 1)[1].strip().strip("'\""))
    passwords.extend(["Fixed-Admin-Pass-2026", "Admin-Pass-2026"])
    return [p for p in passwords if p]


def admin_token() -> str | None:
    """拿到固定管理员令牌，取不到就调重置脚本。"""

    token = _try_login(_candidate_passwords())
    if token:
        return token

    # 现有口令都试不通：调重置脚本。容器内数据库地址已配好，所以走 docker compose exec。
    completed = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend",
         "python", "/app/scripts/reset_admin_password.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO),
    )
    reset_output = (completed.stdout or "") + (completed.stderr or "")
    for line in reset_output.splitlines():
        if "口令已重置为" in line:
            password = line.split("口令已重置为：")[-1].strip()
            if password:
                return _try_login([password])

    # 兜底：宿主机直接跑重置脚本（需要 DATABASE_URL 指向本机）
    import os

    env = dict(os.environ)
    env["DATABASE_URL"] = env.get("DATABASE_URL", "").replace("@postgres:", "@127.0.0.1:")
    host_reset = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "reset_admin_password.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO), env=env,
    )
    host_output = (host_reset.stdout or "") + (host_reset.stderr or "")
    for line in host_output.splitlines():
        if "口令已重置为" in line:
            password = line.split("口令已重置为：")[-1].strip()
            if password:
                return _try_login([password])
    return None


def _try_login(passwords: list[str]) -> str | None:
    for password in passwords:
        status, body = call(
            "POST", "/auth/login", {"username": "root_admin", "password": password}
        )
        if status == 200:
            return body["access_token"]
    return None


SAMPLE_DOC = {
    "filename": "验收样本-增值税优惠公告.txt",
    "text": """财政部 税务总局公告2024年第11号

关于增值税小规模纳税人减免增值税政策的公告

第一章 总则

第一条 对月销售额10万元以下（含本数）的增值税小规模纳税人，免征增值税。

（一）纳税人发生销售行为，未发生应税行为的，不征收增值税。

第二条 增值税小规模纳税人适用3%征收率的应税销售收入，减按1%征收率征收增值税。

第二章 附则

第三条 本公告自2024年1月1日起施行。
""",
    "source_url": "https://www.chinatax.gov.cn/verify/sample.html",
}


def _collect_levels(nodes: list[dict]) -> set[str]:
    levels: set[str] = set()
    for node in nodes:
        levels.add(node.get("level_code", ""))
        levels |= _collect_levels(node.get("children", []))
    return levels


def _first_article_id(nodes: list[dict]) -> str:
    for node in nodes:
        if node.get("level_code") == "article":
            return node["id"]
        found = _first_article_id(node.get("children", []))
        if found:
            return found
    return ""


def _no_overlap(versions: list[dict]) -> bool:
    """检查同一批版本的时间区间是否两两重叠。"""

    ordered = sorted(versions, key=lambda v: v["valid_from"])
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = previous.get("valid_to")
        if previous_end is None:
            return False  # 仍在有效期内的版本后面不该再有版本
        if previous_end > current["valid_from"]:
            return False
    return True


def main() -> int:
    print("=" * 64)
    print("验收：财税知识结构化")
    print("=" * 64)

    token = admin_token()
    if not token:
        print("[FAIL] 无法登录固定管理员，后续检查全部跳过")
        return 1

    status, _ = call("GET", "/knowledge/regulations", token=token)
    check("知识库列表接口已挂载", status == 200, f"HTTP {status}")

    status, _ = call(
        "POST", "/knowledge/regulations/import", {"documents": [SAMPLE_DOC]}
    )
    check("未登录不能导入法规", status in (401, 403), f"HTTP {status}")

    status, body = call(
        "POST", "/knowledge/regulations/import", {"documents": [SAMPLE_DOC]}, token=token
    )
    check("批量导入接口可用", status == 200, f"HTTP {status}")
    if status != 200:
        print(f"       返回：{body}")
        print(f"\n结果：{sum(results)}/{len(results)} 通过")
        return 1

    check("导入报告含进度字段", body.get("progress_percent") == 100.0, str(body.get("progress_percent")))
    check(
        "导入成功或幂等跳过",
        (body.get("success", 0) + body.get("skipped", 0)) >= 1,
        f"success={body.get('success')} skipped={body.get('skipped')}",
    )
    check("导入失败数为 0", body.get("failed") == 0, str(body.get("failed")))

    status, listing = call("GET", "/knowledge/regulations", token=token)
    check("法规列表可查询", status == 200, f"HTTP {status}")
    items = listing.get("items", [])
    check("列表非空", len(items) > 0, f"{len(items)} 条")

    sample = next(
        (i for i in items if i.get("document_number") == "财政部 税务总局公告2024年第11号"),
        None,
    )
    check("验收样本已入库", sample is not None)

    if sample:
        check(
            "元数据：发文机关",
            sample.get("issuer") == "财政部 税务总局",
            str(sample.get("issuer")),
        )
        check(
            "元数据：效力位阶",
            sample.get("hierarchy_level") == "normative_document",
            str(sample.get("hierarchy_level")),
        )
        check(
            "元数据：施行日期",
            str(sample.get("effective_date") or "")[:10] == "2024-01-01",
            str(sample.get("effective_date")),
        )

        status, detail = call("GET", f"/knowledge/regulations/{sample['id']}", token=token)
        check("法规详情接口可用", status == 200, f"HTTP {status}")
        articles = detail.get("articles", [])
        check("条文树非空", len(articles) > 0, f"{len(articles)} 个顶层节点")

        levels = _collect_levels(articles)
        check("识别出章级", "chapter" in levels, str(sorted(levels)))
        check("识别出条级", "article" in levels, str(sorted(levels)))
        check("识别出款级", "paragraph" in levels, str(sorted(levels)))

        article_id = _first_article_id(articles)
        status, versions_body = call(
            "GET", f"/knowledge/articles/{article_id}/versions", token=token
        )
        check("条文版本接口可用", status == 200, f"HTTP {status}")
        versions = versions_body.get("versions", [])
        check(
            "每个版本都有生效日",
            all(v.get("valid_from") for v in versions),
            f"{len(versions)} 个版本",
        )
        check("版本区间不重叠", _no_overlap(versions), f"{len(versions)} 个版本")

    status, repealed = call(
        "GET", "/knowledge/regulations?effect_status=repealed", token=token
    )
    check("按已废止过滤可用", status == 200, f"HTTP {status}")
    check(
        "已废止列表不包含生效法规",
        all(i.get("effect_status") == "repealed" for i in repealed.get("items", [])),
    )

    status, _ = call("GET", "/knowledge/regulations?effect_status=不存在的状态", token=token)
    check("非法效力状态被拒", status == 400, f"HTTP {status}")

    print("=" * 64)
    passed = sum(results)
    print(f"结果：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

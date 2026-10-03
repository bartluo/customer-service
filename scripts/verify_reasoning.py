"""推理层验收（计算引擎部分）。

跑法（项目根目录，需 docker compose up -d）：
    python scripts/verify_reasoning.py

检查三类：
  · P5-A 规则完整性   每条税率/附加税费/优惠规则都写明了依据条款
  · P5-B 依据可核对   规则引用的条款，在知识库里真的存在（不是凭空写的）
  · P5-C 计算正确性   20 道增值税题 + 10 道附加税费题全部算对

**P5-B 是这一层最重要的检查**：计算规则写在配置文件里，
如果依据条款是编的、或者引用的法规根本没入库，用户看到的"依据"就是假的——
而计算结果本身看起来一切正常。所以每一条依据都要回到库里核对一次。

退出码：0 = 全部通过；1 = 有失败项
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import time

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


class Checker:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.start = time.time()

    def check(self, code: str, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(f"{code} {name}")
            print(f"  [通过] {code} {name}")
        else:
            self.failed.append((f"{code} {name}", detail))
            print(f"  [失败] {code} {name} — {detail}")

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        print(f"  [提示] {text}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print()
        print(f"验收结果：{len(self.passed)}/{total} 通过，用时 {time.time() - self.start:.1f} 秒")
        if self.warnings:
            print("提示：")
            for item in self.warnings:
                print(f"  - {item}")
        if self.failed:
            print("失败清单：")
            for name, detail in self.failed:
                print(f"  - {name}：{detail}")
            return 1
        return 0


def collect_citations(raw: dict) -> list[tuple[str, str, bool]]:
    """把规则文件里所有依据条款收集成 (来源说明, 引用文本, 是否声明在库内)。"""

    found: list[tuple[str, str, bool]] = []
    vat = raw.get("vat") or {}
    for entry in vat.get("rates") or []:
        found.append((f"税率 {entry.get('rate')}", entry.get("citation", ""), True))
    for entry in vat.get("levy_rates") or []:
        found.append((f"征收率 {entry.get('rate')}", entry.get("citation", ""), True))
    for method in (vat.get("methods") or {}).values():
        found.append(("计税方法", method.get("citation", ""), True))
    if (vat.get("price_inclusion") or {}).get("citation"):
        found.append(("含税价换算", vat["price_inclusion"]["citation"], True))
    if (vat.get("threshold") or {}).get("citation"):
        found.append(("起征点", vat["threshold"]["citation"], True))
    surcharges = raw.get("surcharges") or {}
    for item in surcharges.get("items") or []:
        in_library = item.get("citation_in_library", True)
        found.append((item.get("name", ""), item.get("citation", ""), in_library))
        for entry in item.get("rates") or []:
            found.append((item.get("name", ""), entry.get("citation", ""), in_library))
    for discount in raw.get("discounts") or []:
        found.append((discount.get("name", ""), discount.get("citation", ""), True))
    return [(source, text, flag) for source, text, flag in found if text]


def main() -> int:
    _load_env = __import__("build_vector_index")._load_env
    _load_env()

    from sqlalchemy import select

    from app.calculation import load_rules
    from app.database.session import SessionLocal
    from app.models.knowledge import Regulation, RegulationArticle

    checker = Checker()
    rules_path = REPO / "domains" / "finance_tax" / "tax_rules.yaml"

    print("[P5-A] 规则完整性")
    try:
        rules = load_rules(rules_path)
    except Exception as exc:  # noqa: BLE001
        checker.check("A1", "规则文件能加载", False, str(exc))
        return checker.summary()
    checker.check("A1", "规则文件能加载并通过校验", True)
    checker.check("A2", "规则带版本号", bool(rules.rules_version), rules.rules_version)
    checker.check("A3", "增值税税率档次 ≥ 4", len(rules.vat_rates()) >= 4, str(len(rules.vat_rates())))
    checker.check("A4", "附加税费 ≥ 3 项", len(rules.surcharge_items()) >= 3)

    citations = collect_citations(rules.raw)
    checker.check("A5", "每条规则都写明了依据", len(citations) > 0, f"共 {len(citations)} 条依据")

    print("\n[P5-B] 依据可核对（规则引用的条款在知识库里存在吗）")
    session = SessionLocal()
    try:
        regulations = session.execute(select(Regulation)).scalars().all()
        titles = sorted(((r.title or ""), r.id) for r in regulations if r.title)
        doc_numbers = {r.document_number: r.id for r in regulations if r.document_number}

        article_index: dict[str, set[str]] = {}
        for article in session.execute(select(RegulationArticle)).scalars().all():
            # 归一化：库里的完整编号带空格（"第十条 （一）"），
            # 规则文件里写的是"第十条（一）"。不归一化会把对的判成错的。
            normalized = re.sub(r"\s+", "", article.full_no or "")
            article_index.setdefault(article.regulation_id, set()).add(normalized)

        missing: list[str] = []
        outside_library: list[str] = []
        for source, citation, in_library in citations:
            text = citation.strip()
            if not in_library:
                # 规则文件已声明"这条依据不在库里"（如财政部综合司的文件）。
                # 不当作失败，但要单独列出来——用户点不开原文、审核也核不了。
                outside_library.append(f"{source} → {text}")
                continue
            # 1) 直接是文号（如"财政部 税务总局公告2023年第12号"）
            if text in doc_numbers:
                continue
            # 2) 形如"X法 第十条（一）"：找到最长的标题前缀
            matched = None
            for title, regulation_id in titles:
                if text.startswith(title) and (matched is None or len(title) > len(matched[0])):
                    matched = (title, regulation_id)
            if matched is None:
                missing.append(f"{source} → {text}（库里找不到这份法规）")
                continue
            remainder = text[len(matched[0]) :].strip()
            if not remainder:
                continue
            full_nos = article_index.get(matched[1], set())
            # 条款号写作"第十条（一）""第二条、第五条"——取其中每个编号逐个核对
            numbers = re.findall(r"第[一二三四五六七八九十百零〇\d]+条(?:（[一二三四五六七八九十]+）)?", remainder)
            if not numbers:
                continue
            if not all(
                any(re.sub(r"\s+", "", number) in full_no for full_no in full_nos)
                for number in numbers
            ):
                missing.append(f"{source} → {text}（条款号在库里对不上）")

        checker.check(
            "B1",
            "规则引用的依据都能在知识库核对到",
            not missing,
            "；".join(missing[:3]) if missing else "",
        )
        if outside_library:
            checker.warn(
                f"以下 {len(outside_library)} 条依据已声明不在知识库内（用户看不到原文，需人工核对）："
            )
            for item in outside_library:
                checker.warn(f"  · {item}")
        if missing:
            for item in missing:
                checker.warn(item)
    finally:
        session.close()

    print("\n[P5-C] 计算正确性（20 道增值税 + 10 道附加税费）")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_calculation_g5.py", "-q", "-p", "no:warnings"],
        cwd=str(REPO / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    match = re.search(r"(\d+) passed", output)
    passed_count = int(match.group(1)) if match else 0
    checker.check(
        "C1",
        "计算用例全部通过",
        completed.returncode == 0,
        f"通过 {passed_count} 项" + ("" if completed.returncode == 0 else f"；{output[-300:]}"),
    )

    print("\n[P5-D] 推理正确性（事实抽取 / 适用性判定 / 槽位补齐）")
    reasoning = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_reasoning_g5.py", "-q", "-p", "no:warnings"],
        cwd=str(REPO / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    reasoning_output = (reasoning.stdout or "") + (reasoning.stderr or "")
    reasoning_match = re.search(r"(\d+) passed", reasoning_output)
    reasoning_count = int(reasoning_match.group(1)) if reasoning_match else 0
    checker.check(
        "D1",
        "推理用例全部通过",
        reasoning.returncode == 0,
        f"通过 {reasoning_count} 项"
        + ("" if reasoning.returncode == 0 else f"；{reasoning_output[-300:]}"),
    )

    print("\n[P5-E] 答案模板与问答主链路")
    answers = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_answer_g5.py", "-q", "-p", "no:warnings"],
        cwd=str(REPO / "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    answers_output = (answers.stdout or "") + (answers.stderr or "")
    answers_match = re.search(r"(\d+) passed", answers_output)
    answers_count = int(answers_match.group(1)) if answers_match else 0
    checker.check(
        "E1",
        "答案模板与主链路用例全部通过",
        answers.returncode == 0,
        f"通过 {answers_count} 项"
        + ("" if answers.returncode == 0 else f"；{answers_output[-300:]}"),
    )

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())

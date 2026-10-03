"""筹划引擎演示：画像 → 手法匹配 → 四道合法性检查 → 风险分级。

用法（项目根目录，需 docker compose up -d）：
    python scripts/plan.py --profile 示例画像.json
    python scripts/plan.py --demo              # 用内置示例跑一遍

说明：
  · 筹划功能的总开关在 domains/finance_tax/risk_rules.yaml（planning_enabled）。
    资质主体落实前保持关闭——本脚本只做**内部推演**，不产出对外方案。
  · 手法素材当前是 draft（待专家确认）状态，脚本会明确标出来。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


DEMO_PROFILE = {
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
    "evidence_available": ["服务合同", "履约记录", "开票与收款流水"],
    "business_authentic": True,
}


def _build_profile(data: dict):
    """把字典转成画像。金额归一与未知字段校验统一在 CompanyProfile.from_mapping，
    命令行、接口、脚本三处共用一份口径。"""

    from app.planning import CompanyProfile

    return CompanyProfile.from_mapping(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="筹划引擎演示")
    parser.add_argument("--profile", help="企业画像 JSON 文件")
    parser.add_argument("--demo", action="store_true", help="用内置示例画像")
    parser.add_argument("--request", default="", help="用户原始诉求（用于红线检查）")
    args = parser.parse_args()

    if args.profile:
        data = json.loads(pathlib.Path(args.profile).read_text(encoding="utf-8"))
    else:
        data = DEMO_PROFILE

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal
    from app.planning import (
        LegalityChecker,
        PlanningSpaceGenerator,
        RedLineMatcher,
        ReviewDesk,
        build_comparison,
        dispatch,
    )
    from app.planning.techniques import parse_citation

    profile = _build_profile(data)
    problems = profile.validate()
    missing = profile.missing_required()

    print("=" * 70)
    print("① 企业画像校验")
    if problems:
        for item in problems:
            print(f"   [不合法] {item}")
    else:
        print("   取值合法")
    if missing:
        print("   还缺以下信息（缺了不能出方案）：")
        for item in missing:
            print(f"     · {item}")
    else:
        print("   关键信息齐全")

    matcher = RedLineMatcher()
    print(f"\n② 筹划总开关：{'开启' if matcher.planning_enabled else '关闭（资质主体落实前保持关闭）'}")

    session = SessionLocal()
    try:
        generator = PlanningSpaceGenerator(session)
        plans = generator.generate(profile)

        print(f"\n③ 筹划空间生成（共 {len(plans)} 个候选方案）")
        by_path: dict[str, list] = {}
        for plan in plans:
            by_path.setdefault(plan.path, []).append(plan)
        for path in ("A", "B", "C"):
            group = by_path.get(path) or []
            if not group:
                continue
            print(f"   路径 {path}（{group[0].path_name}）：{len(group)} 个")
            for plan in group:
                flag = "[待补信息]" if plan.missing else ""
                print(f"     · [{plan.risk_level}] {plan.name} {flag}")
                if plan.missing:
                    print(f"        缺：{'；'.join(plan.missing)}")
                if plan.actions:
                    print(f"        动作：{'；'.join(plan.actions)}")

        print("\n④ 量化测算（数字全部来自计算引擎）")
        for plan in plans:
            m = plan.measurement
            if m.computable:
                print(
                    f"   · {plan.name}：{m.tax_type} {m.before} → {m.after}，"
                    f"节税 {m.saving} 元/年"
                )
                if m.note:
                    print(f"        前提：{m.note}")
            else:
                print(f"   · {plan.name}：无法测算——{m.note}")

        print("\n⑤ 多方案对比")
        comparison = build_comparison(plans)
        for row in comparison["rows"]:
            print(f"   · {row['name']}（{row['path_name']}）")
            print(
                f"        节税：{row['saving_text']}　风险：{row['risk_level']}"
                f"　成本：{row['cost']}　建议：{row['advice']}"
            )
        print("   组合提示：")
        for note in comparison["combinations"]:
            print(f"     - {note}")

        print("\n⑥ 四道合法性检查")
        checker = LegalityChecker(session)
        pairs = [
            parse_citation(citation)
            for plan in plans
            for citation in (plan.citations or [])
        ]
        report = checker.check(
            profile,
            request_text=args.request or str(data.get("goal_detail") or ""),
            citations=pairs,
        )
        print(report.explain())

        print("\n⑦ 输出控制与高风险不外泄")
        level = matcher.risk_level_config(report.risk_level)
        print(f"   整体风险等级：{report.risk_level}（{level.get('name', '')}）")
        result = dispatch(plans, matcher=matcher, profile=profile)
        print(result.explain())

        # 推入复核队列（🔴 只能进这里，🟡/🟢 抄送）
        desk = ReviewDesk(session)
        delivered_codes = {plan.code for plan in result.user_facing}
        for plan in result.review_queue:
            desk.enqueue(
                plan,
                profile=profile,
                question=args.request or str(data.get("goal_detail") or ""),
                delivered_to_user=plan.code in delivered_codes,
            )
        print(f"\n已推入复核队列：{len(result.review_queue)} 条"
              f"（其中 {len(result.withheld)} 条不发给用户）")

        print("\n⑧ 复核台")
        stats = desk.workload()
        print(f"   待办：{stats['pending']}　已处理：{stats['decided']}"
              f"　平均时长：{stats['avg_hours']} 小时")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

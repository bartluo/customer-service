"""筹划与专家复核台接口。

对应技术方案 12.4：
    POST /api/v1/planning/analyze       提交企业画像，得到方案对比
    GET  /api/v1/planning/tasks/{id}    查询进度（本版同步返回，见下）
    GET  /api/v1/planning/reviews       复核待办
    POST /api/v1/planning/reviews/{id}/decision  放行 / 修改后放行 / 驳回

**关于"异步返回任务 ID"（规格 12.4 的要求）**：本版是同步返回的。
理由是筹划分析在本地规则引擎上跑一次约 1～3 秒，引入任务队列只会让
"提交 → 轮询 → 取结果"多三个来回，还多一处会失败的地方；而规格要求异步的
真实原因是"耗时操作不能阻塞调用方"，这个原因在 1～3 秒的量级上不成立。
等接入 LLM 生成方案（单次数十秒）时再改异步，届时任务表与回调一起加。
**这是对规格的一处有意偏离，已记入 ADR-0019。**

**关于筹划总开关（`planning_enabled`，ADR-0015）**：
开关关闭时，方案**不发给普通用户**（返回里 plans 为空并说明原因），
但持有复核权限的审核专家仍能看到方案内容——开关管的是"对外产出"，
不是"内部不能推演"。否则资质落实前复核台没有数据可看，专家也无从准备。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dependencies import CurrentPrincipal, DatabaseSession, require_permission
from app.domain.permissions import (
    CLIENT_PROFILE_WRITE,
    PLANNING_REVIEW,
    PLANNING_REVIEW_HIGH_RISK,
)
from app.planning import (
    CompanyProfile,
    LegalityChecker,
    PlanningSpaceGenerator,
    ProfileError,
    RedLineMatcher,
    ReviewDesk,
    build_comparison,
    dispatch,
)
from app.planning.profile import (
    ENTITY_TYPES,
    FLEXIBILITY_FIELDS,
    GOALS,
    RISK_PREFERENCES,
    TAXPAYER_TYPES,
)
from app.planning.review import ACTION_APPROVE, ACTION_APPROVE_WITH_CHANGES, ACTIONS
from app.planning.techniques import parse_citation
from app.schemas.planning import (
    ComparisonOut,
    FeedbackForEvalResponse,
    PlanOut,
    PlanningAnalyzeRequest,
    PlanningAnalyzeResponse,
    ReviewDecisionRequest,
    ReviewItemOut,
    ReviewListResponse,
    ReviewWorkloadResponse,
)
from app.services import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/planning", tags=["筹划"])

DISCLAIMER = (
    "方案依据现行有效政策与您的画像生成，节税数字由计算引擎给出。"
    "方案能否落地取决于您的实际经营安排，实施前建议与主管税务机关沟通确认。"
)


def _to_plan_out(plan) -> PlanOut:
    return PlanOut(**plan.to_dict())


def _to_review_out(item) -> ReviewItemOut:
    return ReviewItemOut(**item.to_dict())


@router.get(
    "/profile-schema",
    summary="企业画像的字段与允许取值（前端表单用）",
)
def profile_schema(principal: CurrentPrincipal) -> dict:
    """把画像的可选值开放出来，前端不用把枚举抄一遍。

    抄一遍的后果很具体：以后加一个组织形式（例如"农民专业合作社"），
    后端加了、前端没加，用户在选择框里根本选不到，报错还说是取值不合法。
    """

    return {
        "money_fields": list(CompanyProfile.MONEY_FIELDS),
        "enums": {
            "entity_type": list(ENTITY_TYPES),
            "taxpayer_type": list(TAXPAYER_TYPES),
            "risk_preference": list(RISK_PREFERENCES),
            "goal": list(GOALS),
        },
        "flexibility_fields": list(FLEXIBILITY_FIELDS),
        "required_for_plan": [
            "entity_type",
            "taxpayer_type",
            "goal",
            "risk_preference",
            "flexible",
            "business_authentic",
        ],
        "note": "金额字段可传字符串或数字，服务端按 Decimal 处理；"
        "business_purpose / business_benefit 由用户自己填写，系统不代为编造",
    }


@router.post(
    "/analyze",
    response_model=PlanningAnalyzeResponse,
    summary="提交企业画像，生成筹划方案对比",
    dependencies=[Depends(require_permission(CLIENT_PROFILE_WRITE))],
)
def analyze(
    payload: PlanningAnalyzeRequest,
    session: DatabaseSession,
    principal: CurrentPrincipal,
) -> PlanningAnalyzeResponse:
    try:
        profile = CompanyProfile.from_mapping(payload.profile)
    except ProfileError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"画像字段有误：{exc}"
        ) from exc

    problems = profile.validate()
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="；".join(problems),
        )

    missing = profile.missing_required()
    matcher = RedLineMatcher()
    planning_enabled = matcher.planning_enabled
    notes: list[str] = []

    generator = PlanningSpaceGenerator(session)
    plans = generator.generate(profile)

    checker = LegalityChecker(session)
    pairs = [parse_citation(citation) for plan in plans for citation in (plan.citations or [])]
    legality = checker.check(
        profile,
        request_text=payload.request_text or (profile.goal_detail or ""),
        citations=pairs,
    )

    result = dispatch(plans, matcher=matcher, profile=profile)

    # 谁能看到方案：开关开着 → 所有人；开关关着 → 只有审核专家（内部推演）
    can_see = planning_enabled or principal.has(PLANNING_REVIEW)
    if not planning_enabled:
        notes.append(
            "筹划功能总开关当前为关闭状态（资质主体落实前保持关闭），"
            "方案不会对外产出；以下内容仅对审核专家可见，用于内部推演与准备。"
        )
    if missing:
        notes.append("画像还缺关键信息，方案可行性可能不完整：" + "；".join(missing))

    created_reviews = 0
    if payload.enqueue_review and planning_enabled:
        desk = ReviewDesk(session)
        delivered = {plan.code for plan in result.user_facing}
        for plan in result.review_queue:
            desk.enqueue(
                plan,
                profile=profile,
                question=payload.request_text or (profile.goal_detail or ""),
                delivered_to_user=plan.code in delivered,
            )
            created_reviews += 1

    audit.record(
        session,
        action="planning.analyze",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=principal.tenant_id,
        target_type="planning_analysis",
        detail={
            "goal": profile.goal,
            "entity_type": profile.entity_type,
            "candidate_count": len(plans),
            "user_facing": len(result.user_facing),
            "withheld": len(result.withheld),
            "risk_level": legality.risk_level,
            "planning_enabled": planning_enabled,
            "created_reviews": created_reviews,
        },
    )
    session.commit()

    user_facing = [_to_plan_out(plan) for plan in result.user_facing] if can_see else []
    withheld = [_to_plan_out(plan) for plan in result.withheld] if can_see else []

    return PlanningAnalyzeResponse(
        profile=profile.to_dict(),
        missing_required=missing,
        planning_enabled=planning_enabled,
        plans=user_facing,
        withheld=withheld,
        comparison=ComparisonOut(**build_comparison(result.user_facing if can_see else [])),
        legality=legality.to_dict(),
        risk_level=legality.risk_level,
        notes=notes,
        created_reviews=created_reviews,
        disclaimer=DISCLAIMER,
    )


@router.get(
    "/reviews",
    response_model=ReviewListResponse,
    summary="复核待办列表",
    dependencies=[Depends(require_permission(PLANNING_REVIEW))],
)
def list_reviews(
    session: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> ReviewListResponse:
    """一屏看清一条待办：画像 + 方案 + 依据 + 测算 + 风险点。"""

    items = ReviewDesk(session).pending(limit=limit)
    return ReviewListResponse(total=len(items), items=[_to_review_out(item) for item in items])


@router.get(
    "/reviews/workload",
    response_model=ReviewWorkloadResponse,
    summary="复核工作量（待办 / 平均时长 / 按时率 / 超时）",
    dependencies=[Depends(require_permission(PLANNING_REVIEW))],
)
def review_workload(session: DatabaseSession) -> ReviewWorkloadResponse:
    desk = ReviewDesk(session)
    stats = desk.workload()
    overdue = desk.overdue()
    return ReviewWorkloadResponse(
        pending=stats["pending"],
        decided=stats["decided"],
        avg_hours=stats["avg_hours"],
        sla_hours=stats["sla_hours"],
        on_time_rate=stats["on_time_rate"],
        overdue=[_to_review_out(desk.get(row.id)) for row in overdue if desk.get(row.id)],
    )


@router.get(
    "/reviews/feedback",
    response_model=FeedbackForEvalResponse,
    summary="待转评测集的反馈清单（被否案例素材）",
    dependencies=[Depends(require_permission(PLANNING_REVIEW))],
)
def review_feedback(session: DatabaseSession) -> FeedbackForEvalResponse:
    """驳回与修改后放行的案例，整理成待转清单交专家确认。

    刻意不自动写进手法库：手法库是专家资产，自动追加会让未经确认的判断
    混进权威数据里（定下的口径，见 ADR-0015）。
    """

    items = ReviewDesk(session).feedback_for_eval()
    return FeedbackForEvalResponse(total=len(items), items=items)


@router.post(
    "/reviews/{review_id}/decision",
    response_model=ReviewItemOut,
    summary="作出复核决定（放行 / 修改后放行 / 驳回）",
)
def decide_review(
    review_id: str,
    payload: ReviewDecisionRequest,
    session: DatabaseSession,
    principal: CurrentPrincipal,
) -> ReviewItemOut:
    """作出复核决定。

    高风险方案的放行需要单独的高风险权限（技术方案 10.4 两级授权）。

    把关的人自己就能给高风险方案开口子，等于这条红线只写在文档里。
    """

    if payload.action not in ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"未知复核动作：{payload.action}（可选：{' / '.join(ACTIONS)}）",
        )

    desk = ReviewDesk(session)
    item = desk.get(review_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="复核任务不存在")

    needs_high_risk = payload.action in (ACTION_APPROVE, ACTION_APPROVE_WITH_CHANGES)
    if needs_high_risk:
        target_level = payload.risk_level or item.risk_level
        if target_level == "red":
            if not principal.has(PLANNING_REVIEW_HIGH_RISK):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="放行高风险方案需要高风险管理权限",
                )
        elif not principal.has(PLANNING_REVIEW):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="缺少权限：planning.review"
            )
    elif not principal.has(PLANNING_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="缺少权限：planning.review"
        )

    changes: dict = {}
    if payload.risk_level:
        changes["risk_level"] = {"to": payload.risk_level}
    if payload.note:
        changes["note"] = {"to": payload.note}

    desk.decide(
        review_id,
        action=payload.action,
        reviewer=principal.username,
        note=payload.note,
        changes=changes,
    )

    audit.record(
        session,
        action="planning.review_decision",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=principal.tenant_id,
        target_type="planning_review",
        target_id=review_id,
        detail={
            "action": payload.action,
            "risk_level": payload.risk_level,
            "note": payload.note[:500],
            "technique_code": item.review.technique_code,
        },
    )
    session.commit()

    updated = desk.get(review_id)
    if updated is None:  # 理论上不会发生：刚改完就查不到说明写入被回滚了
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="复核结果写入失败"
        )
    return _to_review_out(updated)

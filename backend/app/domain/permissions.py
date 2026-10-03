"""权限码常量与内置角色定义。

这份文件是技术方案 10.5 权限表的代码落地，改这里等于改规格，需同步改文档。

三条设计规则：
  1. 默认拒绝：权限表里没有的，一律不放行。
  2. delegable=False 的权限只有固定管理员能给（技术方案 10.4 两级授权）。
  3. 内置三个角色不可删除；审核专家角色普通管理员不能授。
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionSpec:
    code: str
    name_zh: str
    category: str
    delegable: bool
    sensitive: bool = False
    description: str = ""


@dataclass(frozen=True)
class RoleSpec:
    code: str
    name_zh: str
    description: str
    grantable_by_admin: bool
    permissions: tuple[str, ...]


# ---------- 权限码 ----------
TENANT_MANAGE = "tenant.manage"
USER_MANAGE = "user.manage"
PERMISSION_GRANT = "permission.grant"
AUDIT_VIEW = "audit.view"
SYSTEM_CONFIG = "system.config"
INDEX_REBUILD = "index.rebuild"

KNOWLEDGE_IMPORT = "knowledge.import"
KNOWLEDGE_REVIEW = "knowledge.review"
KNOWLEDGE_UPDATE = "knowledge.update"
KNOWLEDGE_GAP_HANDLE = "knowledge.gap.handle"
PLANNING_REVIEW = "planning.review"
PLANNING_REVIEW_HIGH_RISK = "planning.review.high_risk"
REDLINE_MANAGE = "redline.manage"

QA_ASK = "qa.ask"
CITATION_VIEW = "citation.view"
CALC_RUN = "calc.run"
CALC_DRAFT_SAVE = "calc.draft.save"
ANSWER_EXPORT = "answer.export"
FEEDBACK_SUBMIT = "feedback.submit"
CLIENT_PROFILE_WRITE = "client.profile.write"


PERMISSIONS: tuple[PermissionSpec, ...] = (
    # —— 管理类：普通管理员不可转授 ——
    PermissionSpec(TENANT_MANAGE, "租户管理", "管理", delegable=False,
                   description="创建、修改、停用租户"),
    PermissionSpec(USER_MANAGE, "用户管理", "管理", delegable=False,
                   description="创建、停用用户，重置他人口令"),
    PermissionSpec(PERMISSION_GRANT, "分配角色与权限", "管理", delegable=False,
                   description="给用户授予或收回角色与权限"),
    PermissionSpec(AUDIT_VIEW, "查看审计日志", "管理", delegable=False,
                   description="查看登录、授权、知识变更等操作记录"),
    PermissionSpec(SYSTEM_CONFIG, "系统配置", "管理", delegable=False,
                   description="修改系统级配置与功能开关"),
    PermissionSpec(INDEX_REBUILD, "重建检索索引", "管理", delegable=False,
                   description="触发向量索引重建"),
    # —— 审核类：仅审核专家与固定管理员 ——
    PermissionSpec(KNOWLEDGE_IMPORT, "导入法规与知识", "审核", delegable=False,
                   sensitive=True, description="导入法规原文与知识单元"),
    PermissionSpec(KNOWLEDGE_REVIEW, "审核发布知识", "审核", delegable=False,
                   sensitive=True, description="审核并发布知识单元，决定其是否生效"),
    PermissionSpec(KNOWLEDGE_UPDATE, "修正已发布知识", "审核", delegable=False,
                   sensitive=True, description="直接修正已发布的知识内容"),
    PermissionSpec(KNOWLEDGE_GAP_HANDLE, "处理知识缺口", "审核", delegable=False,
                   description="处理系统识别出的知识缺口"),
    PermissionSpec(PLANNING_REVIEW, "复核筹划方案（🟡）", "审核", delegable=False,
                   sensitive=True, description="复核审慎级筹划方案"),
    PermissionSpec(PLANNING_REVIEW_HIGH_RISK, "放行 🔴 高风险方案", "审核", delegable=False,
                   sensitive=True, description="放行高风险筹划方案，红线方案不可放行"),
    PermissionSpec(REDLINE_MANAGE, "维护红线清单", "审核", delegable=False,
                   description="维护不可触碰的违法做法清单"),
    # —— 使用类：可转授 ——
    PermissionSpec(QA_ASK, "提问", "使用", delegable=True,
                   description="向系统提问并获得答案"),
    PermissionSpec(CITATION_VIEW, "查看依据与条文原文", "使用", delegable=True,
                   description="查看答案引用的法规条文原文"),
    PermissionSpec(CALC_RUN, "使用税费计算器", "使用", delegable=True,
                   description="运行税费计算，结果不落库"),
    PermissionSpec(CALC_DRAFT_SAVE, "保存计算底稿", "使用", delegable=True,
                   sensitive=True, description="把计算结果保存为底稿"),
    PermissionSpec(ANSWER_EXPORT, "导出答案", "使用", delegable=True,
                   sensitive=True, description="导出答案与依据，导出件自动带免责声明"),
    PermissionSpec(FEEDBACK_SUBMIT, "提交反馈", "使用", delegable=True,
                   description="对答案提交纠错与满意度反馈"),
    PermissionSpec(CLIENT_PROFILE_WRITE, "填写企业画像", "使用", delegable=True,
                   sensitive=True, description="填写企业基本信息，用于筹划建议"),
)


# ---------- 内置角色 ----------
ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLE_CLIENT = "client"

_MANAGEMENT_PERMISSIONS = (
    TENANT_MANAGE, USER_MANAGE, PERMISSION_GRANT, AUDIT_VIEW, SYSTEM_CONFIG, INDEX_REBUILD,
)
_REVIEW_PERMISSIONS = (
    KNOWLEDGE_IMPORT, KNOWLEDGE_REVIEW, KNOWLEDGE_UPDATE, KNOWLEDGE_GAP_HANDLE,
    PLANNING_REVIEW, PLANNING_REVIEW_HIGH_RISK, REDLINE_MANAGE,
)
_USAGE_PERMISSIONS = (
    QA_ASK, CITATION_VIEW, CALC_RUN, CALC_DRAFT_SAVE, ANSWER_EXPORT,
    FEEDBACK_SUBMIT, CLIENT_PROFILE_WRITE,
)


ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(
        code=ROLE_ADMIN,
        name_zh="管理员",
        description="管账号、管租户、发权限；不参与业务审核",
        grantable_by_admin=True,
        permissions=_MANAGEMENT_PERMISSIONS + (QA_ASK, CITATION_VIEW, CALC_RUN, FEEDBACK_SUBMIT),
    ),
    RoleSpec(
        code=ROLE_REVIEWER,
        name_zh="审核专家",
        description="专业内容把关：审核知识、复核筹划方案、放行高风险方案",
        # 只有固定管理员能授予此角色（技术方案 10.4）
        grantable_by_admin=False,
        permissions=_REVIEW_PERMISSIONS + (QA_ASK, CITATION_VIEW, CALC_RUN, ANSWER_EXPORT, FEEDBACK_SUBMIT),
    ),
    RoleSpec(
        code=ROLE_CLIENT,
        name_zh="客户",
        description="用系统解决实际问题的企业人员",
        grantable_by_admin=True,
        permissions=_USAGE_PERMISSIONS,
    ),
)


PERMISSION_CODES: frozenset[str] = frozenset(spec.code for spec in PERMISSIONS)
ROLE_CODES: frozenset[str] = frozenset(spec.code for spec in ROLES)


def is_valid_permission(code: str) -> bool:
    return code in PERMISSION_CODES


def is_valid_role(code: str) -> bool:
    return code in ROLE_CODES

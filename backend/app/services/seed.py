"""种子数据：权限项、内置角色、固定管理员。

幂等：可重复执行，已存在的记录不重复插入（靠唯一约束 + 先查后写）。
"""

from __future__ import annotations
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.permissions import PERMISSIONS, ROLES
from app.models import Permission, Role, RolePermission, User, UserRole
from app.security.passwords import generate_password, hash_password, password_problems

logger = logging.getLogger(__name__)


def seed_permissions(db: Session) -> int:
    """写入全部权限项。返回新增数量。"""

    existing = set(db.execute(select(Permission.code)).scalars().all())
    created = 0
    for spec in PERMISSIONS:
        if spec.code in existing:
            continue
        db.add(
            Permission(
                code=spec.code,
                name_zh=spec.name_zh,
                category=spec.category,
                description=spec.description or None,
                delegable=spec.delegable,
                sensitive=spec.sensitive,
            )
        )
        created += 1
    db.flush()
    return created


def seed_roles(db: Session) -> int:
    """写入三个内置角色及其默认权限。"""

    role_by_code: dict[str, Role] = {}
    created = 0
    for spec in ROLES:
        role = db.execute(select(Role).where(Role.code == spec.code)).scalar_one_or_none()
        if role is None:
            role = Role(
                code=spec.code,
                name_zh=spec.name_zh,
                description=spec.description,
                is_builtin=True,
                grantable_by_admin=spec.grantable_by_admin,
            )
            db.add(role)
            created += 1
        role_by_code[spec.code] = role
    db.flush()

    permission_by_code = {
        code: perm_id
        for code, perm_id in db.execute(select(Permission.code, Permission.id)).all()
    }
    for spec in ROLES:
        role = role_by_code[spec.code]
        existing_pairs = set(
            db.execute(
                select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
            ).scalars().all()
        )
        for code in spec.permissions:
            perm_id = permission_by_code.get(code)
            if perm_id is None:
                # 权限码写错时立刻暴露，不静默跳过
                raise RuntimeError(f"角色 {spec.code} 引用了不存在的权限码：{code}")
            if perm_id not in existing_pairs:
                db.add(RolePermission(role_id=role.id, permission_id=perm_id))
    db.flush()
    return created


def ensure_protected_admin(db: Session, settings: Settings) -> tuple[User, bool, str | None]:
    """确保固定管理员存在。

    返回 (用户, 是否新建, 初始口令)。
    新建时若 BOOTSTRAP_ADMIN_PASSWORD 为空，则生成随机口令并由调用方打印一次。
    """

    existing = db.execute(select(User).where(User.is_protected.is_(True))).scalar_one_or_none()
    if existing is not None:
        return existing, False, None

    username = settings.bootstrap_admin_username or "root_admin"
    password = settings.bootstrap_admin_password
    if password:
        problems = password_problems(
            password, min_length=settings.bootstrap_admin_password_min_length
        )
        if problems and settings.is_production:
            raise RuntimeError(
                "生产环境的固定管理员口令不合规：" + "；".join(problems)
            )
        if problems:
            logger.warning("固定管理员口令不合规（开发环境放行）：%s", "；".join(problems))
    else:
        password = generate_password()

    user = User(
        username=username,
        # 用 example.com 而不是 local.invalid：.invalid 是 RFC 保留的测试域名，
        # 邮箱校验器会判定它不可投递并直接拒收，导致登录接口 500。
        # 固定管理员不用真实邮箱，登录只认用户名。
        email=f"{username}@example.com",
        display_name="固定管理员",
        password_hash=hash_password(password),
        tenant_id=None,
        is_protected=True,
        is_active=True,
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=_admin_role_id(db), granted_by=user.id))
    db.flush()
    return user, True, password


def _admin_role_id(db: Session) -> str:
    role_id = db.execute(select(Role.id).where(Role.code == "admin")).scalar_one()
    return role_id


def run_seeds(db: Session, settings: Settings) -> dict[str, object]:
    """执行全部种子。返回一份结果摘要，供启动日志与接口返回。"""

    permission_count = seed_permissions(db)
    role_count = seed_roles(db)
    admin, admin_created, initial_password = ensure_protected_admin(db, settings)
    db.commit()
    return {
        "permissions_created": permission_count,
        "roles_created": role_count,
        "protected_admin_created": admin_created,
        "protected_admin_username": admin.username,
        "initial_password": initial_password,
    }

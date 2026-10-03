"""域包加载器：启动时扫描 domains/ 目录，校验并注册所有域包。

设计要点：
  · 严格校验：manifest 写错时立刻报错并指出具体字段，不静默跳过。
    静默跳过会让"域包没加载"变成一个查不到原因的问题。
  · 路由关键词做互斥检查：两个域都声称匹配同一关键词时告警。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.domain_packs.schema import (
    COMPONENT_NAMES,
    VALID_STATUSES,
    ComponentSpec,
    DomainPackManifest,
    RoutingHints,
)

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.yaml"


class DomainPackError(Exception):
    """域包相关错误的基类。"""


class ManifestValidationError(DomainPackError):
    """manifest 内容不合法。message 里必须说清"哪个域包的哪个字段有问题"。"""


@dataclass(frozen=True)
class DomainPack:
    """加载后的域包：清单 + 所在目录。"""

    manifest: DomainPackManifest
    directory: Path

    @property
    def domain_id(self) -> str:
        return self.manifest.domain_id

    @property
    def is_active(self) -> bool:
        return self.manifest.status == "active"

    def file_path(self, relative: str) -> Path:
        """返回域包内某个文件的绝对路径，并阻止越界访问（防止 ../../etc/passwd）。"""

        root = self.directory.resolve()
        target = (self.directory / relative).resolve()
        if not str(target).startswith(str(root)):
            raise DomainPackError(f"{self.domain_id} 试图访问域包目录之外的路径：{relative}")
        return target


def _require(data: dict, key: str, domain_label: str) -> object:
    if key not in data:
        raise ManifestValidationError(f"{domain_label} 缺少必填字段 {key}")
    return data[key]


def _parse_components(raw: object, domain_label: str) -> dict[str, ComponentSpec]:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"{domain_label} 的 components 必须是对象")
    unknown = set(raw) - set(COMPONENT_NAMES)
    if unknown:
        raise ManifestValidationError(
            f"{domain_label} 的 components 含未知组件：{sorted(unknown)}；"
            f"合法组件为 {list(COMPONENT_NAMES)}"
        )
    missing = set(COMPONENT_NAMES) - set(raw)
    if missing:
        raise ManifestValidationError(f"{domain_label} 的 components 缺少组件：{sorted(missing)}")

    components: dict[str, ComponentSpec] = {}
    for name in COMPONENT_NAMES:
        value = raw[name]
        # 简写形式：纯字符串表示"声明了但还没做"
        if isinstance(value, str):
            components[name] = ComponentSpec(status=value)
            continue
        if not isinstance(value, dict):
            raise ManifestValidationError(
                f"{domain_label} 的 components.{name} 必须是字符串或对象"
            )
        status = value.get("status", "pending")
        if status not in {"pending", "ready"}:
            raise ManifestValidationError(
                f"{domain_label} 的 components.{name}.status 只能是 pending 或 ready，"
                f"当前为 {status!r}"
            )
        path = value.get("path")
        if status == "ready" and not path:
            raise ManifestValidationError(
                f"{domain_label} 的 components.{name} 标为 ready 但没有写 path"
            )
        components[name] = ComponentSpec(status=status, path=path, note=value.get("note", ""))
    return components


def parse_manifest(data: dict, directory: Path) -> DomainPack:
    """把已解析的 YAML 字典转成 DomainPack，并做完整校验。"""

    domain_id = str(_require(data, "domain_id", "manifest"))
    label = f"域包 {domain_id}"

    if not domain_id.replace("_", "").isalnum():
        raise ManifestValidationError(
            f"{label} 的 domain_id 只能包含字母、数字和下划线，当前为 {domain_id!r}"
        )

    status = str(data.get("status", "planned"))
    if status not in VALID_STATUSES:
        raise ManifestValidationError(
            f"{label} 的 status 只能是 {sorted(VALID_STATUSES)}，当前为 {status!r}"
        )

    roles_raw = _require(data, "roles", label)
    if not isinstance(roles_raw, list) or not all(isinstance(item, str) for item in roles_raw):
        raise ManifestValidationError(f"{label} 的 roles 必须是字符串列表")

    components = _parse_components(_require(data, "components", label), label)

    routing_raw = data.get("routing", {}) or {}
    if not isinstance(routing_raw, dict):
        raise ManifestValidationError(f"{label} 的 routing 必须是对象")

    def as_tuple(key: str) -> tuple[str, ...]:
        value = routing_raw.get(key, []) or []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ManifestValidationError(f"{label} 的 routing.{key} 必须是字符串列表")
        return tuple(value)

    routing = RoutingHints(
        keywords=as_tuple("keywords"),
        include=as_tuple("include"),
        exclude=as_tuple("exclude"),
    )

    features_raw = data.get("features", {}) or {}
    if not isinstance(features_raw, dict) or not all(
        isinstance(v, bool) for v in features_raw.values()
    ):
        raise ManifestValidationError(f"{label} 的 features 必须是布尔值映射")

    scope_raw = data.get("scope", {}) or {}
    if not isinstance(scope_raw, dict):
        raise ManifestValidationError(f"{label} 的 scope 必须是对象")

    tax_batches_raw = data.get("tax_batches", {}) or {}
    if not isinstance(tax_batches_raw, dict):
        raise ManifestValidationError(f"{label} 的 tax_batches 必须是对象")
    for batch, items in tax_batches_raw.items():
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ManifestValidationError(
                f"{label} 的 tax_batches.{batch} 必须是字符串列表"
            )

    manifest = DomainPackManifest(
        domain_id=domain_id,
        name=str(_require(data, "name", label)),
        version=str(data.get("version", "0.0.1")),
        status=status,
        description=str(data.get("description", "")),
        roles=tuple(roles_raw),
        components=components,
        routing=routing,
        features=features_raw,
        scope=scope_raw,
        tax_batches=tax_batches_raw,
    )
    return DomainPack(manifest=manifest, directory=directory)


def load_pack(directory: Path) -> DomainPack:
    """加载单个域包目录。"""

    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise ManifestValidationError(f"域包目录缺少 {MANIFEST_FILENAME}：{directory}")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestValidationError(f"{directory} 的 manifest 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ManifestValidationError(f"{directory} 的 manifest 顶层必须是对象")
    return parse_manifest(data, directory)


def check_ready_files(pack: DomainPack) -> list[str]:
    """标为 ready 的组件，其 path 必须真实存在。"""

    problems: list[str] = []
    for name in pack.manifest.ready_components():
        relative = pack.manifest.components[name].path
        if not relative:
            continue
        if not pack.file_path(relative).exists():
            problems.append(f"{pack.domain_id}.{name} 标为 ready 但文件不存在：{relative}")
    return problems


def find_routing_conflicts(packs: list[DomainPack]) -> list[str]:
    """两个域抢同一个路由关键词 → 告警（不阻断，但必须让人知道）。"""

    owner: dict[str, str] = {}
    conflicts: list[str] = []
    for pack in packs:
        for keyword in pack.manifest.routing.keywords:
            if keyword in owner and owner[keyword] != pack.domain_id:
                conflicts.append(
                    f"路由关键词 {keyword!r} 同时被 {owner[keyword]} 与 {pack.domain_id} 声明"
                )
            owner.setdefault(keyword, pack.domain_id)
    return conflicts


def load_all_packs(domains_root: Path) -> dict[str, DomainPack]:
    """扫描目录加载所有域包。任何一个不合法就整体报错，不静默跳过。

    返回 {domain_id: DomainPack}。
    """

    if not domains_root.exists():
        raise DomainPackError(f"域包根目录不存在：{domains_root}")

    packs: dict[str, DomainPack] = {}
    for directory in sorted(domains_root.iterdir()):
        if not directory.is_dir():
            continue
        if directory.name.startswith((".", "_")):
            continue
        if not (directory / MANIFEST_FILENAME).exists():
            # 没有 manifest 的目录不算域包（例如误建的空目录），跳过但记一条日志
            logger.info("跳过无 manifest 的目录：%s", directory.name)
            continue
        pack = load_pack(directory)
        if pack.domain_id in packs:
            raise ManifestValidationError(
                f"domain_id 重复：{pack.domain_id} 同时出现在 "
                f"{packs[pack.domain_id].directory.name} 与 {directory.name}"
            )
        # 目录名应与 domain_id 一致，否则排障时容易找错地方
        if directory.name != pack.domain_id:
            raise ManifestValidationError(
                f"目录名 {directory.name!r} 与 domain_id {pack.domain_id!r} 不一致"
            )
        packs[pack.domain_id] = pack

    if not packs:
        raise DomainPackError(f"在 {domains_root} 下没有找到任何域包")

    problems: list[str] = []
    for pack in packs.values():
        problems.extend(check_ready_files(pack))
    if problems:
        raise ManifestValidationError("域包组件文件缺失：\n  · " + "\n  · ".join(problems))

    for conflict in find_routing_conflicts(list(packs.values())):
        logger.warning("域路由冲突：%s", conflict)

    return packs

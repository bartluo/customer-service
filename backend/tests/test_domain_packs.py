"""域包加载器与域路由的测试（不依赖数据库）。

重点验证两件事：
  1. 写错的 manifest 会被抓住并说清哪里错了（验收标准）
  2. 路由能把财税问题分到财税域、把其他领域的问题分到对应域（验收标准）
"""

from __future__ import annotations
import shutil
import textwrap
from pathlib import Path

import pytest
import yaml

from app.domain_packs.loader import (
    DomainPack,
    ManifestValidationError,
    find_routing_conflicts,
    load_all_packs,
    load_pack,
)
from app.domain_packs.registry import DomainRegistry
from app.domain_packs.router import route
from app.domain_packs.schema import COMPONENT_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_DOMAINS = REPO_ROOT / "domains"


def _write_pack(root: Path, domain_id: str, manifest: dict, extra_files: dict | None = None) -> Path:
    directory = root / domain_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    for name, content in (extra_files or {}).items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(content, encoding="utf-8")
    return directory


def _valid_manifest(domain_id: str = "test_domain", **overrides) -> dict:
    manifest = {
        "domain_id": domain_id,
        "name": "测试域",
        "version": "0.1.0",
        "status": "active",
        "description": "用于测试的域包",
        "roles": ["client"],
        "components": {name: "pending" for name in COMPONENT_NAMES},
        "routing": {"keywords": ["测试"], "include": [], "exclude": []},
        "features": {"demo": True},
    }
    manifest.update(overrides)
    return manifest


def _sandbox_with_second_domain(tmp_path: Path) -> list[DomainPack]:
    """复制真实 domains 目录，再临时加一个域，用于验证跨域路由。"""

    sandbox = tmp_path / "domains"
    shutil.copytree(REAL_DOMAINS, sandbox)
    _write_pack(
        sandbox,
        "legal_affairs",
        _valid_manifest(
            domain_id="legal_affairs",
            name="法务",
            routing={"keywords": ["合同", "诉讼", "侵权"], "include": [], "exclude": []},
        ),
    )
    return list(load_all_packs(sandbox).values())


# ---------- 真实域包能加载 ----------
def test_real_domain_packs_load() -> None:
    packs = load_all_packs(REAL_DOMAINS)
    assert "finance_tax" in packs
    finance = packs["finance_tax"]
    assert finance.is_active
    assert finance.manifest.name == "财税"
    # 九个组件必须都声明了
    assert set(finance.manifest.components) == set(COMPONENT_NAMES)
    # 已就位的组件
    assert "ontology" in finance.manifest.ready_components()


def test_domain_id_matches_directory_name() -> None:
    packs = load_all_packs(REAL_DOMAINS)
    for pack in packs.values():
        assert pack.domain_id == pack.directory.name


def test_ready_components_have_real_files() -> None:
    for pack in load_all_packs(REAL_DOMAINS).values():
        for name in pack.manifest.ready_components():
            relative = pack.manifest.components[name].path
            assert pack.file_path(relative).exists(), f"{pack.domain_id}.{name} 声明 ready 但文件缺失"


def test_finance_tax_roles_use_three_builtin_codes() -> None:
    """角色码必须与定稿的三个内置角色一致（10.1）。"""

    finance = load_all_packs(REAL_DOMAINS)["finance_tax"]
    assert set(finance.manifest.roles) <= {"admin", "reviewer", "client"}


# ---------- 校验：每个错误都要被抓住并说清位置 ----------
def test_missing_required_field_is_reported(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    del manifest["domain_id"]
    _write_pack(tmp_path, "test_domain", manifest)
    with pytest.raises(ManifestValidationError, match="缺少必填字段"):
        load_all_packs(tmp_path)


def test_invalid_status_is_reported(tmp_path: Path) -> None:
    _write_pack(tmp_path, "test_domain", _valid_manifest(status="running"))
    with pytest.raises(ManifestValidationError, match="status 只能是"):
        load_all_packs(tmp_path)


def test_unknown_component_is_reported(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["components"]["magic_module"] = "pending"
    _write_pack(tmp_path, "test_domain", manifest)
    with pytest.raises(ManifestValidationError, match="未知组件"):
        load_all_packs(tmp_path)


def test_missing_component_is_reported(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    del manifest["components"]["ontology"]
    _write_pack(tmp_path, "test_domain", manifest)
    with pytest.raises(ManifestValidationError, match="缺少组件"):
        load_all_packs(tmp_path)


def test_ready_component_without_path_is_reported(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["components"]["ontology"] = {"status": "ready"}
    _write_pack(tmp_path, "test_domain", manifest)
    with pytest.raises(ManifestValidationError, match="标为 ready 但没有写 path"):
        load_all_packs(tmp_path)


def test_ready_component_missing_file_is_reported(tmp_path: Path) -> None:
    manifest = _valid_manifest()
    manifest["components"]["ontology"] = {"status": "ready", "path": "ontology.yaml"}
    _write_pack(tmp_path, "test_domain", manifest)  # 不创建 ontology.yaml
    with pytest.raises(ManifestValidationError, match="文件不存在"):
        load_all_packs(tmp_path)


def test_directory_name_mismatch_is_reported(tmp_path: Path) -> None:
    _write_pack(tmp_path, "wrong_dir_name", _valid_manifest(domain_id="test_domain"))
    with pytest.raises(ManifestValidationError, match="与 domain_id"):
        load_all_packs(tmp_path)


def test_duplicate_domain_id_is_reported(tmp_path: Path) -> None:
    _write_pack(tmp_path, "test_domain", _valid_manifest())
    # 第二个目录名与 domain_id 不一致会更早报错，所以直接用同一 domain_id 的另一个目录模拟冲突
    second = tmp_path / "test_domain_copy"
    second.mkdir()
    (second / "manifest.yaml").write_text(
        yaml.safe_dump(_valid_manifest(), allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(ManifestValidationError):
        load_all_packs(tmp_path)


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "test_domain"
    directory.mkdir()
    (directory / "manifest.yaml").write_text("domain_id: [unclosed", encoding="utf-8")
    with pytest.raises(ManifestValidationError, match="不是合法 YAML"):
        load_all_packs(tmp_path)


def test_non_dict_manifest_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "test_domain"
    directory.mkdir()
    (directory / "manifest.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ManifestValidationError, match="顶层必须是对象"):
        load_all_packs(tmp_path)


def test_no_packs_found_is_reported(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="没有找到任何域包"):
        load_all_packs(tmp_path)


def test_invalid_domain_id_chars_are_reported(tmp_path: Path) -> None:
    _write_pack(tmp_path, "test-domain", _valid_manifest(domain_id="test-domain"))
    with pytest.raises(ManifestValidationError):
        load_all_packs(tmp_path)


def test_file_path_blocks_directory_escape(tmp_path: Path) -> None:
    pack = load_pack(_write_pack(tmp_path, "test_domain", _valid_manifest()))
    with pytest.raises(Exception, match="域包目录之外"):
        pack.file_path("../../etc/passwd")


# ---------- 注册中心 ----------
def test_registry_load_is_idempotent() -> None:
    registry = DomainRegistry()
    first = registry.load(REAL_DOMAINS)
    second = registry.load(REAL_DOMAINS)
    assert first == second == 1
    assert registry.is_loaded()


def test_registry_summary_shape() -> None:
    registry = DomainRegistry()
    registry.load(REAL_DOMAINS)
    summary = {item["domain_id"]: item for item in registry.summary()}
    assert "finance_tax" in summary
    assert "ready_components" in summary["finance_tax"]
    assert isinstance(summary["finance_tax"]["pending_components"], list)


def test_routing_conflict_is_detected(tmp_path: Path) -> None:
    _write_pack(tmp_path, "alpha", _valid_manifest(domain_id="alpha", routing={"keywords": ["共同词"]}))
    _write_pack(tmp_path, "beta", _valid_manifest(domain_id="beta", routing={"keywords": ["共同词"]}))
    packs = load_all_packs(tmp_path)
    conflicts = find_routing_conflicts(list(packs.values()))
    assert conflicts and "共同词" in conflicts[0]


# ---------- 域路由 ----------
def test_tax_question_routes_to_finance_tax(tmp_path: Path) -> None:
    packs = _sandbox_with_second_domain(tmp_path)
    result = route("小规模纳税人增值税怎么算？需要申报吗", packs)
    assert result.domain_id == "finance_tax", result.reason


def test_other_domain_question_routes_to_that_domain(tmp_path: Path) -> None:
    packs = _sandbox_with_second_domain(tmp_path)
    result = route("这份合同的违约条款怎么主张", packs)
    assert result.domain_id == "legal_affairs", result.reason


def test_planned_domain_does_not_participate_in_routing(tmp_path: Path) -> None:
    sandbox = tmp_path / "domains"
    shutil.copytree(REAL_DOMAINS, sandbox)
    _write_pack(
        sandbox,
        "legal_affairs",
        _valid_manifest(
            domain_id="legal_affairs",
            name="法务",
            status="planned",
            routing={"keywords": ["合同", "诉讼"], "include": [], "exclude": []},
        ),
    )
    packs = list(load_all_packs(sandbox).values())
    result = route("这份合同的违约条款怎么主张", packs)
    assert result.domain_id != "legal_affairs"


def test_exclude_beats_include(tmp_path: Path) -> None:
    """命中 exclude 的域，即使同时命中关键词也被排除。"""

    _write_pack(
        tmp_path,
        "alpha",
        _valid_manifest(
            domain_id="alpha",
            routing={"keywords": ["服务"], "include": ["专业服务"], "exclude": ["咨询"]},
        ),
    )
    _write_pack(
        tmp_path,
        "beta",
        _valid_manifest(
            domain_id="beta",
            routing={"keywords": ["咨询"], "include": [], "exclude": []},
        ),
    )
    packs = list(load_all_packs(tmp_path).values())
    result = route("专业服务咨询怎么做", packs)
    assert result.scores.get("alpha", 0) == 0.0


def test_unmatched_question_asks_for_clarification() -> None:
    packs = list(load_all_packs(REAL_DOMAINS).values())
    result = route("今天天气怎么样", packs)
    assert result.domain_id is None
    assert result.needs_clarification or "没有" in result.reason


def test_tenant_domain_filter_blocks_other_domains(tmp_path: Path) -> None:
    """租户只订阅财税，财税问题能路由，其他域不参与。"""

    packs = _sandbox_with_second_domain(tmp_path)
    result = route("增值税怎么算", packs, tenant_domains=["finance_tax"])
    assert result.domain_id == "finance_tax"
    blocked = route("这份合同的违约条款怎么主张", packs, tenant_domains=["finance_tax"])
    assert blocked.domain_id != "legal_affairs"


def test_tenant_with_no_domains_matches_nothing() -> None:
    packs = list(load_all_packs(REAL_DOMAINS).values())
    result = route("增值税怎么算", packs, tenant_domains=[])
    assert result.domain_id is None
    assert "未订阅" in result.reason or "没有可用" in result.reason

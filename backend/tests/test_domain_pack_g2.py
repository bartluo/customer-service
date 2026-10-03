"""验收门 G2：新增一个域包不需要改引擎代码。

这是域包架构最核心的承诺，必须用自动化测试锁住。
测试做法：临时造一个新域包目录，加载它、路由到它，然后再删掉。
全过程不碰任何引擎代码。
"""

from __future__ import annotations
import shutil
from pathlib import Path

import yaml

from app.domain_packs.loader import load_all_packs
from app.domain_packs.registry import DomainRegistry
from app.domain_packs.router import route
from app.domain_packs.schema import COMPONENT_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_domain_pack(root: Path, domain_id: str, name: str, keywords: list[str]) -> Path:
    """在 root 下造一个最小可用域包。这是"新增一个域"的全部动作。"""

    directory = root / domain_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "domain_id": domain_id,
        "name": name,
        "version": "0.1.0",
        "status": "active",
        "description": f"{name}测试域包",
        "roles": ["client"],
        "components": {component: "pending" for component in COMPONENT_NAMES},
        "routing": {"keywords": keywords, "include": [], "exclude": []},
        "features": {},
    }
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return directory


def _sandbox(tmp_path: Path) -> Path:
    """复制一份真实 domains 目录，避免测试污染仓库。"""

    sandbox = tmp_path / "domains"
    shutil.copytree(REPO_ROOT / "domains", sandbox)
    return sandbox


def test_new_domain_pack_needs_no_engine_code_change(tmp_path: Path) -> None:
    """把新域包目录放进 domains/ 就该被自动加载并可路由。"""

    sandbox = _sandbox(tmp_path)

    # 改动前：只有财税一个域
    before = load_all_packs(sandbox)
    assert set(before) == {"finance_tax"}

    # 唯一动作：新建一个目录 + 一个 manifest.yaml
    _make_domain_pack(sandbox, "legal_affairs", "法务", ["合同", "诉讼", "侵权"])

    # 改动后：自动多了一个域，引擎代码一行没动
    after = load_all_packs(sandbox)
    assert set(after) == {"finance_tax", "legal_affairs"}
    assert after["legal_affairs"].manifest.name == "法务"

    # 且能路由到它
    result = route("这份合同的违约条款怎么主张", list(after.values()))
    assert result.domain_id == "legal_affairs", result.reason

    # 原有域的路由没被影响
    tax = route("增值税怎么算", list(after.values()))
    assert tax.domain_id == "finance_tax", tax.reason


def test_registry_hot_loads_new_pack(tmp_path: Path) -> None:
    """注册中心能重新加载，新增域立刻可见。"""

    sandbox = _sandbox(tmp_path)

    registry = DomainRegistry()
    assert registry.load(sandbox) == 1
    assert registry.get("medical") is None

    _make_domain_pack(sandbox, "medical", "医疗", ["诊断", "处方", "病历"])
    registry.load(sandbox, force=True)

    assert registry.get("medical") is not None
    assert len(registry.all_packs()) == 2
    result = route("这个处方能开吗", registry.all_packs())
    assert result.domain_id == "medical", result.reason


def test_disabling_a_pack_removes_it_from_routing(tmp_path: Path) -> None:
    """把域包状态改成 disabled，路由层立刻不再选它。"""

    sandbox = _sandbox(tmp_path)
    _make_domain_pack(sandbox, "legal_affairs", "法务", ["合同", "诉讼"])

    packs = load_all_packs(sandbox)
    assert route("合同纠纷怎么办", list(packs.values())).domain_id == "legal_affairs"

    # 改状态为 disabled
    manifest_path = sandbox / "legal_affairs" / "manifest.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["status"] = "disabled"
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    packs = load_all_packs(sandbox)
    result = route("合同纠纷怎么办", list(packs.values()))
    assert result.domain_id != "legal_affairs"

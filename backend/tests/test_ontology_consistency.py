"""域包 yaml 与代码枚举的一致性测试。

为什么需要：效力状态是"废止条文绝不能进答案"这条硬规则的输入。
它同时写在 domains/finance_tax/ontology.yaml 和 backend/app/knowledge/ontology.py 两处。
两处一旦漂移，检索硬过滤与审核台会各按各的判断，静默出错且很难发现。
这个测试把"两处必须一致"变成硬约束。
"""

from __future__ import annotations
import pathlib

import pytest
import yaml

from app.knowledge import ontology as code_ontology

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FINANCE_TAX_DOMAIN = REPO_ROOT / "domains" / "finance_tax"


def _load_yaml(name: str) -> dict:
    path = FINANCE_TAX_DOMAIN / name
    if not path.exists():
        pytest.skip(f"域包文件不存在：{path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_effect_statuses_match_yaml() -> None:
    """六种效力状态的 code 与顺序必须与 ontology.yaml 完全一致。"""

    config = _load_yaml("ontology.yaml")
    yaml_codes = tuple(item["code"] for item in config["effect_statuses"])
    assert yaml_codes == code_ontology.EFFECT_STATUSES


def test_hierarchy_levels_match_yaml() -> None:
    """六级效力位阶必须一致。"""

    config = _load_yaml("ontology.yaml")
    yaml_codes = tuple(item["code"] for item in config["hierarchy_levels"])
    assert yaml_codes == code_ontology.HIERARCHY_LEVELS


def test_granularity_levels_match_yaml() -> None:
    """章/节/条/款/项/目六级粒度必须一致。"""

    config = _load_yaml("ontology.yaml")
    yaml_codes = tuple(item["code"] for item in config["article_granularity"])
    assert yaml_codes == code_ontology.GRANULARITY_LEVELS


def test_relation_types_match_yaml() -> None:
    """关系类型集合必须一致。"""

    config = _load_yaml("ontology.yaml")
    yaml_codes = {item["code"] for item in config["relation_types"]}
    assert yaml_codes == set(code_ontology.RELATION_TYPES)


def test_citable_statuses_are_subset_of_all() -> None:
    """可引用状态必须是六种之一，不能凭空多出。"""

    assert set(code_ontology.CITABLE_EFFECT_STATUSES) <= set(code_ontology.EFFECT_STATUSES)
    assert set(code_ontology.NON_CITABLE_EFFECT_STATUSES) <= set(code_ontology.EFFECT_STATUSES)
    assert set(code_ontology.CONDITIONAL_EFFECT_STATUSES) <= set(code_ontology.EFFECT_STATUSES)


def test_citable_and_non_citable_do_not_overlap() -> None:
    """可引用与禁止引用不得有交集，否则过滤逻辑自相矛盾。"""

    assert not set(code_ontology.CITABLE_EFFECT_STATUSES) & set(
        code_ontology.NON_CITABLE_EFFECT_STATUSES
    )


def test_repealed_superseded_draft_are_not_citable() -> None:
    """ontology.yaml validations.no_cite_repealed 的代码化断言（blocking 级规则）。"""

    for status in ("repealed", "superseded", "draft"):
        assert not code_ontology.is_citable(status)


def test_retrieval_filter_does_not_use_legacy_status() -> None:
    """retrieval.yaml 的硬过滤规则不得再出现旧枚举里的 expired 等状态。"""

    config = _load_yaml("retrieval.yaml")
    raw = (FINANCE_TAX_DOMAIN / "retrieval.yaml").read_text(encoding="utf-8")
    assert "'expired'" not in raw
    assert "'pending'" not in raw
    assert "'amended'" not in raw
    assert config is not None

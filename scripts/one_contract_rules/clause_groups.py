"""Controlled client clause topics; distinct from public modules' free-text titles.

These identifiers restrict where a client rule may be applied. They do not claim
that a contract contains the relevant clause: absent clauses may need insertion.
Changing this vocabulary or its meaning requires a resolver semantics version bump.
"""
from __future__ import annotations

from typing import Any, Mapping


CLAUSE_GROUP_LABELS = {
    "subject": "合同主体与签署权限", "eligibility": "资格与准入条件",
    "scope": "标的与履行范围", "payment": "价款与付款", "tax": "税费与发票",
    "delivery": "交付与交接", "acceptance": "验收", "quality": "质量与标准",
    "warranty": "质保与维修", "ownership": "权属与风险转移", "ip": "知识产权",
    "confidentiality": "保密", "data": "数据与个人信息", "compliance": "合规义务",
    "security": "担保与增信", "liability": "违约与责任承担", "termination": "解除与终止",
    "term": "期限与续期", "dispute": "争议解决", "notice": "通知与送达",
    "assignment": "转让与分包", "labor": "劳动用工", "governance": "治理与决策权限",
}
CLAUSE_GROUP_IDS = tuple(CLAUSE_GROUP_LABELS)


def vocabulary_options() -> list[dict[str, str]]:
    return [{"value": key, "label": value} for key, value in CLAUSE_GROUP_LABELS.items()]


def validate_clause_groups(value: Any, *, field: str = "clause_groups") -> tuple[str, ...]:
    """Require exact registered IDs, without aliases or title-based inference."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(field + " 须为受控条款类别编号列表。")
    if len(set(value)) != len(value):
        raise ValueError(field + " 不得重复选择条款类别。")
    unknown = sorted(set(value) - set(CLAUSE_GROUP_IDS))
    if unknown:
        raise ValueError(field + " 包含未登记的条款类别：" + "、".join(unknown)
                         + "；请明确选择已有类别，不会根据名称猜测。")
    return tuple(value)


def validate_review_scope(context: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Omission means whole-contract review; a subset must be chosen explicitly."""
    mode = context.get("review_scope", "whole_contract")
    if mode not in ("whole_contract", "clause_groups"):
        raise ValueError("review_scope 须为 whole_contract（整合同）或 clause_groups（指定条款类别）。")
    groups = validate_clause_groups(context.get("clause_groups", []))
    if mode == "whole_contract" and groups:
        raise ValueError("整合同审查不得同时限定 clause_groups；请明确切换为指定条款类别审查。")
    if mode == "clause_groups" and not groups:
        raise ValueError("指定条款类别审查须至少选择一个 clause_groups 类别。")
    return mode, groups

#!/usr/bin/env python3
"""Load review plans and enrich only explicit, structured decision fields."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ACTION_ALIAS = {
    "comment": "comment",
    "批注": "comment",
    "注释": "comment",
    "report-only": "report-only",
    "report_only": "report-only",
    "仅报告": "report-only",
    "仅写入意见书": "report-only",
    "意见书": "report-only",
    "replace": "replace",
    "修订": "replace",
    "修改": "replace",
    "insert": "insert",
    "插入": "insert",
    "新增": "insert",
    "delete": "delete",
    "删除": "delete",
    "auto": "auto",
    "自动": "auto",
    "none": "none",
    "skip": "skip",
}
EVIDENCE_ALIASES = {
    "confirmed": "confirmed",
    "已确认": "confirmed",
    "确定": "confirmed",
    "reasonable_assumption": "reasonable_assumption",
    "reasonable-assumption": "reasonable_assumption",
    "合理假设": "reasonable_assumption",
    "major_choice": "major_choice",
    "major-choice": "major_choice",
    "重大选择": "major_choice",
    "not_applicable": "not_applicable",
    "not-applicable": "not_applicable",
    "不适用": "not_applicable",
}
EDIT_POLICIES = {"comment-first", "balanced", "revise-first"}
DIRECT_EDIT_ACTIONS = {"replace", "insert", "delete"}


def load_plan(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("审查计划 JSON 顶层必须是对象")
    return payload


def get_plan_meta(plan: dict[str, Any]) -> dict[str, Any]:
    meta = plan.get("meta")
    return meta if isinstance(meta, dict) else {}


def get_findings(plan: dict[str, Any]) -> list[Any]:
    findings = plan.get("findings") or plan.get("risks") or []
    if not isinstance(findings, list):
        raise ValueError("findings 必须是数组")
    return findings


def _normalize_action(action: Any) -> str:
    raw = str(action or "").strip()
    if not raw:
        return "auto"
    return ACTION_ALIAS.get(raw.lower(), ACTION_ALIAS.get(raw, raw.lower()))


def normalize_edit_policy(policy: Any, default: str = "revise-first") -> str:
    raw = str(policy or "").strip().lower()
    if not raw:
        return default
    if raw not in EDIT_POLICIES:
        raise ValueError(f"不支持的 edit_policy: {policy}")
    return raw


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}
    return False


def _has_direct_edit_payload(finding: dict[str, Any], *, edit_policy: str) -> bool:
    if (
        finding.get("replacement_text") is not None
        or finding.get("insert_text") is not None
        or _is_truthy(finding.get("delete"))
        or _is_truthy(finding.get("remove"))
    ):
        return True
    return edit_policy != "comment-first" and bool(
        str(finding.get("recommended_text") or "").strip()
    )


def infer_evidence_state(
    finding: dict[str, Any], *, edit_policy: str = "revise-first"
) -> str:
    explicit = str(
        finding.get("evidence_state") or finding.get("decision_state") or ""
    ).strip()
    if explicit:
        normalized = EVIDENCE_ALIASES.get(explicit.lower()) or EVIDENCE_ALIASES.get(explicit)
        if not normalized:
            raise ValueError(f"不支持的 evidence_state: {explicit}")
        return normalized
    if _is_truthy(finding.get("not_applicable")):
        return "not_applicable"
    if any(
        _is_truthy(finding.get(key))
        for key in (
            "requires_authorization",
            "needs_negotiation",
            "requires_negotiation",
            "needs_confirmation",
            "requires_confirmation",
        )
    ):
        return "major_choice"
    if finding.get("unknown_facts") or finding.get("assumptions"):
        return "reasonable_assumption"
    action = _normalize_action(finding.get("action"))
    if action in {"none", "skip"}:
        return "not_applicable"
    if action == "comment":
        return "major_choice"
    if action in DIRECT_EDIT_ACTIONS or _has_direct_edit_payload(
        finding, edit_policy=normalize_edit_policy(edit_policy)
    ):
        return "confirmed"
    return "major_choice"


def infer_strategy_flags(
    finding: dict[str, Any], *, edit_policy: str = "revise-first"
) -> dict[str, bool]:
    """Compatibility fields derived from the explicit evidence state."""
    state = infer_evidence_state(finding, edit_policy=edit_policy)
    return {
        "needs_negotiation": state == "major_choice",
        "deterministic_edit": state == "confirmed"
        and _has_direct_edit_payload(
            finding, edit_policy=normalize_edit_policy(edit_policy)
        ),
    }


def enrich_findings(
    findings: list[Any], *, edit_policy: str = "revise-first"
) -> list[Any]:
    policy = normalize_edit_policy(edit_policy)
    enriched: list[Any] = []
    for item in findings:
        if not isinstance(item, dict):
            enriched.append(item)
            continue
        finding = dict(item)
        finding["action"] = _normalize_action(finding.get("action"))
        finding.setdefault("evidence_state", infer_evidence_state(finding, edit_policy=policy))
        inferred = infer_strategy_flags(finding, edit_policy=policy)
        finding.setdefault("needs_negotiation", inferred["needs_negotiation"])
        finding.setdefault("deterministic_edit", inferred["deterministic_edit"])
        enriched.append(finding)
    return enriched


def enrich_plan(
    plan: dict[str, Any], *, edit_policy: str | None = None
) -> dict[str, Any]:
    result = deepcopy(plan)
    meta = get_plan_meta(result)
    policy = normalize_edit_policy(
        edit_policy or meta.get("edit_policy"), default="revise-first"
    )
    meta["edit_policy"] = policy
    result["meta"] = meta
    findings = get_findings(result)
    key = "findings" if "findings" in result or "risks" not in result else "risks"
    result[key] = enrich_findings(findings, edit_policy=policy)
    return result

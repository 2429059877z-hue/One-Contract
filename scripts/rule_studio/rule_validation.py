"""Execute draft scenarios through the production resolver, without publishing.

Assertions are supplied by the reviewer, not copied from the observed result.
Evidence is bound to the saved revision and the public/active snapshot used.
This verifies rule resolution only; it is not a legal or DOCX delivery check.
The compatibility keys ``actual.applies`` and ``expected.rules`` mean inclusion
in the review inputs. Client CLIENT_APPLICATION_REVIEW notices require full-text
AI assessment and real Word comments; inclusion is not final applicability.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from one_contract_rules import client_repository, evidence_records, models, resolver
from one_contract_rules.transaction_lock import profile_lock


class RuleTestError(ValueError):
    pass


class _DraftResolver(resolver.Resolver):
    def __init__(self, *, public_repository, client_repository, profile_id, draft):
        super().__init__(public_repository=public_repository, client_repository=client_repository)
        self.profile_id = profile_id
        profile = client_repository.get_client_profile(profile_id)
        if profile.get("status") != "active":
            raise RuleTestError("客户档案未启用，不能执行测试。")
        active = client_repository.load_active_snapshot(profile_id)
        self.base_snapshot_id = (active or {}).get("snapshot_id")
        active_rules = []
        if active:
            snapshot, enabled, conflicts, human = super()._load_client_overlay(
                profile_id=profile_id, snapshot_id=self.base_snapshot_id,
                expected_fingerprint=public_repository.public_asset_fingerprint)
            if not enabled or human or conflicts:
                raise RuleTestError("当前客户快照不兼容或不可用，请先处理升级/完整性问题。")
            active_rules = list(super()._clients_snapshot_rules(snapshot))
        simulated = copy.deepcopy(draft)
        simulated.update(approval_status="active", activation_status="active")
        simulated["content_sha256"] = models.content_hash(simulated)
        self.rules = [r for r in active_rules if r["client_rule_id"] != draft["client_rule_id"]] + [simulated]
        self.snapshot = {"snapshot_id": "draft-test:" + draft["content_sha256"],
                         "client_profile_id": profile_id}

    def _load_client_overlay(self, *, profile_id, snapshot_id, expected_fingerprint):
        if profile_id != self.profile_id:
            raise RuleTestError("测试不得跨客户执行。")
        return self.snapshot, True, [], False

    def _clients_snapshot_rules(self, snapshot):
        return self.rules


def _validate_case(case: Mapping[str, Any], seen: set) -> None:
    if not isinstance(case, Mapping):
        raise RuleTestError("测试场景必须是结构化对象。")
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 160 or case_id in seen:
        raise RuleTestError("每个测试场景必须有不重复的名称。")
    seen.add(case_id)
    context = case.get("input")
    if not isinstance(context, Mapping) or not all(isinstance(context.get(k), str) and context[k] for k in ("primary_type_id", "our_role")):
        raise RuleTestError("场景须指定合同类型和我方角色。")
    for key in ("scene_tags", "secondary_type_ids", "case_authorizations"):
        if key in context and not isinstance(context[key], list):
            raise RuleTestError("场景字段 " + key + " 必须为列表。")
    if "facts" in context and not isinstance(context["facts"], Mapping):
        raise RuleTestError("事实须按事实编号和实际取值填写。")
    expected = case.get("expected")
    if not isinstance(expected, Mapping) or any(type(expected.get(k)) is not bool for k in ("applies", "human_confirmation_required")):
        raise RuleTestError("须预先明确：是否进入审查候选，以及是否存在需先处理的阻断事项。")
    if set(expected) - {"applies", "human_confirmation_required", "effect"}:
        raise RuleTestError("预期结果包含不支持的字段。")
    effect = expected.get("effect")
    if expected["applies"] and (not isinstance(effect, Mapping) or set(effect) != {"action", "target_key", "value"}):
        raise RuleTestError("预期进入审查候选时须明确该规则的动作、作用目标及值。")
    if not expected["applies"] and "effect" in expected:
        raise RuleTestError("预期不进入审查候选时请移除处理动作。")


def run_rule_tests(*, public_repository, client_repository, profile_id: str,
                   rule_id: str, cases: list, expected_revision: int, actor: str) -> dict:
    if type(expected_revision) is not int or expected_revision < 1:
        raise RuleTestError("请先保存规则并载入最新版本。")
    draft = client_repository.get_draft(profile_id, rule_id)
    if draft is None:
        raise RuleTestError("规则草稿不存在。")
    if draft["revision"] != expected_revision:
        raise RuleTestError("草稿已修改，请重新载入后核对测试预期。")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise RuleTestError("每次须提供 1 至 100 个具有预期结果的测试场景。")
    seen: set = set()
    for case in cases:
        _validate_case(case, seen)
        owner = case["input"].get("client_profile_id")
        if owner is not None and owner != profile_id:
            raise RuleTestError("测试场景的客户与当前草稿不一致。")
    engine = _DraftResolver(public_repository=public_repository, client_repository=client_repository,
                            profile_id=profile_id, draft=draft)
    results = []
    for case in cases:
        context = copy.deepcopy(case["input"])
        context.update(client_profile_id=profile_id, client_policy_snapshot_id=None)
        expected = copy.deepcopy(case["expected"])
        result = engine.resolve_effective_rules(context)
        selected = next((r for r in result.effective_rules if r.rule_id == rule_id and r.source_layer == "client"), None)
        actual = {"applies": selected is not None,
                  "human_confirmation_required": result.human_confirmation_required}
        if selected:
            effect = selected.rule_payload.get("effect") or {}
            actual["effect"] = {key: effect.get(key) for key in ("action", "target_key", "value")}
        results.append({"case_id": case["case_id"], "input": context, "expected": expected,
                        "actual": actual, "passed": actual == expected,
                        "excluded": [dict(x) for x in result.excluded_rules if x.get("rule_id") == rule_id],
                        "conflicts": [dict(x) for x in result.rule_conflicts],
                        "effective_set_hash": result.result_hash})
    report = {"executor": {"name": "one-contract-production-resolver", "version": public_repository.resolver_semantics_version},
              "suite_version": "studio-scenarios-1", "scope": "rule_resolution",
              "public_asset_fingerprint": public_repository.public_asset_fingerprint,
              "base_client_snapshot_id": engine.base_snapshot_id, "cases": results,
              "limits": ["仅验证规则是否进入审查输入、声明的处理动作及阻断要求；applies 不代表客户规则最终适用。", "客户适用性仍须结合合同全文判断并保留真实批注；不代替法律审核、修订合同生成或交付验收。"]}
    evidence = client_repository.store_regression_evidence(profile_id, rule_id, report=report,
                                                          expected_revision=expected_revision, actor=actor)
    passed = all(c["passed"] for c in results)
    updated = client_repository.get_draft(profile_id, rule_id)
    if passed and draft["approval_status"] in ("lawyer_approved", "regression_passed", "active"):
        updated = client_repository.record_regression_result(profile_id, rule_id,
                    evidence_id=evidence["evidence_id"], passed=True, actor=actor, expected_revision=expected_revision)
    return {"report": report, "evidence_id": evidence["evidence_id"], "passed": passed, "draft": updated,
            "release_check_recorded": passed and updated["approval_status"] == "regression_passed"}


class _RuleSetResolver(resolver.Resolver):
    """Resolve a frozen replacement overlay, with no borrowed active rules."""
    def __init__(self, *, public_repository, client_repository, profile_id, drafts, base_snapshot_id):
        super().__init__(public_repository=public_repository, client_repository=client_repository)
        self.profile_id = profile_id
        self.base_snapshot_id = base_snapshot_id
        self.rules = []
        for draft in drafts:
            simulated = copy.deepcopy(draft)
            simulated.update(approval_status="active", activation_status="active")
            simulated["content_sha256"] = models.content_hash(simulated)
            self.rules.append(simulated)
        self.target_set_sha256 = evidence_records.rule_set_hash(self.rules)
        self.snapshot = {"snapshot_id": "draft-set-test:" + self.target_set_sha256,
                         "client_profile_id": profile_id}

    def _load_client_overlay(self, *, profile_id, snapshot_id, expected_fingerprint):
        if profile_id != self.profile_id:
            raise RuleTestError("测试不得跨客户执行。")
        return self.snapshot, True, [], False

    def _clients_snapshot_rules(self, snapshot):
        return self.rules


def run_rule_set_tests(*, public_repository, client_repository, profile_id: str,
                       cases: list, expected_revisions: Mapping[str, int],
                       expected_active_snapshot: str | None, actor: str) -> dict:
    """Execute asserted scenarios against every approved draft as one overlay.

    Revisions cover exactly the complete intended set; unapproved/deprecated
    drafts are explicitly disclosed. The transaction verifies the same set and
    active snapshot after execution, before committing any lifecycle changes.
    """
    if client_repository.read_only:
        raise RuleTestError("只读模式不能保存测试记录。")
    if not isinstance(expected_revisions, Mapping) or not expected_revisions or any(
            type(value) is not int or value < 1 for value in expected_revisions.values()):
        raise RuleTestError("整组测试须提供每条已确认规则的最新版本。")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise RuleTestError("每次须提供 1 至 100 个具有预期结果的测试场景。")
    # Freeze all drafts in a single transaction. Resolver work itself can run
    # outside the lock; the storage commit compares every revision again.
    with profile_lock(client_repository._resolve_profile_dir(profile_id)):
        if client_repository.get_client_profile(profile_id).get("status") != "active":
            raise RuleTestError("客户档案未启用，不能执行测试。")
        active = client_repository.load_active_snapshot(profile_id)
        base_snapshot_id = (active or {}).get("snapshot_id")
        if base_snapshot_id != expected_active_snapshot:
            raise RuleTestError("现行快照已变化，请重新载入后测试（ACTIVE_SNAPSHOT_CONFLICT）。")
        all_drafts = client_repository.list_drafts(profile_id)
        drafts = [d for d in all_drafts if d["approval_status"] in ("lawyer_approved", "regression_passed", "active")]
        if {d["client_rule_id"]: d["revision"] for d in drafts} != dict(expected_revisions):
            raise RuleTestError("规则集合或内容已修改，请重新载入后核对测试预期（DRAFT_REVISION_CONFLICT）。")
        skipped = [{"client_rule_id": d["client_rule_id"], "approval_status": d["approval_status"],
                    "reason": "已停用" if d["approval_status"] == "deprecated" else "尚未人工确认"}
                   for d in all_drafts if d not in drafts]
    seen: set = set()
    for case in cases:
        if not isinstance(case, Mapping):
            raise RuleTestError("测试场景必须是结构化对象。")
        expected = case.get("expected")
        if (not isinstance(expected, Mapping) or set(expected) != {"rules", "human_confirmation_required"}
                or not isinstance(expected["rules"], Mapping)
                or type(expected["human_confirmation_required"]) is not bool
                or set(expected["rules"]) - set(expected_revisions)):
            raise RuleTestError("整组预期须逐一写明应进入审查候选的客户规则及处理动作，并明确是否存在阻断事项。")
        for effect in expected["rules"].values():
            if not isinstance(effect, Mapping) or set(effect) != {"action", "target_key", "value"}:
                raise RuleTestError("每条预期规则须明确动作、作用目标及实际值。")
        # Reuse the input/name contract; assertions use the complete set shape.
        _validate_case({**case, "expected": {"applies": False, "human_confirmation_required": False}}, seen)
        owner = case["input"].get("client_profile_id")
        if owner is not None and owner != profile_id:
            raise RuleTestError("测试场景的客户与当前集合不一致。")
    engine = _RuleSetResolver(public_repository=public_repository, client_repository=client_repository,
        profile_id=profile_id, drafts=drafts, base_snapshot_id=base_snapshot_id)
    results = []
    for case in cases:
        context = copy.deepcopy(case["input"])
        context.update(client_profile_id=profile_id, client_policy_snapshot_id=None)
        result = engine.resolve_effective_rules(context)
        effects = {}
        for selected in result.effective_rules:
            if selected.source_layer == "client":
                effect = selected.rule_payload.get("effect") or {}
                effects[selected.rule_id] = {key: effect.get(key) for key in ("action", "target_key", "value")}
        actual = {"rules": effects, "human_confirmation_required": result.human_confirmation_required}
        expected = copy.deepcopy(case["expected"])
        results.append({"case_id": case["case_id"], "input": context, "expected": expected,
                        "actual": actual, "passed": actual == expected,
                        "excluded": [dict(x) for x in result.excluded_rules if x.get("rule_id") in expected_revisions],
                        "conflicts": [dict(x) for x in result.rule_conflicts], "effective_set_hash": result.result_hash})
    report = {"executor": {"name": "one-contract-production-resolver-batch", "version": public_repository.resolver_semantics_version},
              "suite_version": "studio-set-scenarios-1", "scope": "rule_set_resolution",
              "target_set_sha256": engine.target_set_sha256,
              "public_asset_fingerprint": public_repository.public_asset_fingerprint,
              "base_client_snapshot_id": base_snapshot_id, "cases": results,
              "limits": ["仅验证目标集合是否进入审查输入、声明的处理动作及阻断要求；rules 不代表客户规则最终适用。",
                         "未覆盖的事实组合仍需补充场景；不代替法律审核、合同生成或交付验收。"]}
    stored = client_repository.store_rule_set_evidence(profile_id, report=report,
        expected_revisions=expected_revisions, expected_active_snapshot=base_snapshot_id, actor=actor)
    return {**stored, "report": report, "skipped_drafts": skipped}

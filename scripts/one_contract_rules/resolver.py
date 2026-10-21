#!/usr/bin/env python3
"""公共＋客户规则合成。

契约来源：architecture-contract.md §7（解析算法）、§6（EffectiveRuleSet）、
§8.5（哈希）、§14（兼容与降级边界）；ADR-009（优先级按性质）、
ADR-010（审查任务固定快照）、ADR-020（硬边界只做确定性 guard）、
ADR-022（公共升级默认暂停客户 overlay）、ADR-028（降级边界）。

硬边界：
- **未选择客户**时结果必须与纯公共选择器一致（RES-024 / INT-008）；
- 一旦**明确选择客户**，snapshot 缺失、损坏或 public fingerprint 不兼容时
  **必须 fail closed**，不得静默退回公共规则（DATA-006 / ADR-028）；
- 客户业务适用性和商业冲突交由持有合同全文的 AI 审查，须在真实 Word 批注中
  说明判断、事实不足或商业选择；不得因此伪造人工确认或绕过法律硬边界。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from . import clause_groups, guard_catalog, models, validator

__all__ = [
    "Resolver",
    "ResolverError",
    "is_application_review_notice",
    "resolution_has_blockers",
]

#: 优先级排序：按性质，不按数值（ADR-009）
_CLASS_ORDER = tuple(models.NORMATIVE_CLASSES)
_LAYER_ORDER = ("global", "domain", "type", "client", "case_instruction")
_APPLICATION_REVIEW_KINDS = frozenset({
    "scope_mismatch", "scope_undetermined", "condition_unmatched", "condition_undetermined",
    "semantic_condition", "rule_confirmation", "explicit_conflict", "value_divergence",
    "authorization_missing", "case_authorized_override",
})


def is_application_review_notice(conflict: Any) -> bool:
    """Recognise only the controlled client-business review contract.

    An arbitrary ``blocking: false`` on a guard, snapshot failure or public
    conflict is never authority to continue.
    """
    if not isinstance(conflict, Mapping):
        return False
    rule_ids = conflict.get("rule_ids")
    return bool(
        conflict.get("code") == "CLIENT_APPLICATION_REVIEW"
        and conflict.get("kind") in _APPLICATION_REVIEW_KINDS
        and conflict.get("source_layer") == "client"
        and conflict.get("blocking") is False
        and conflict.get("requires_word_comment") is True
        and conflict.get("assessment_basis") == "full_contract"
        and isinstance(rule_ids, (list, tuple)) and rule_ids
        and all(isinstance(rule_id, str) and rule_id.strip() for rule_id in rule_ids)
    )


def resolution_has_blockers(result: Any) -> bool:
    """Keep storage, identity, public and legal gates distinct from AI review."""
    if isinstance(result, models.EffectiveRuleSet):
        result = result.to_dict()
    if not isinstance(result, Mapping):
        return True
    if result.get("status") == "blocked":
        return True
    if "effective_rule_set" in result and resolution_has_blockers(result["effective_rule_set"]):
        return True
    conflicts = result.get("rule_conflicts", ())
    if not isinstance(conflicts, (list, tuple)):
        return True
    return bool(result.get("human_confirmation_required") or any(
        not is_application_review_notice(item) for item in conflicts
    ))


def _application_review(kind: str, rule_ids: Sequence[str], detail: str, **extra: Any) -> Dict[str, Any]:
    if kind not in _APPLICATION_REVIEW_KINDS:
        raise ValueError("Unknown client application review kind: " + kind)
    return {**extra, "code": "CLIENT_APPLICATION_REVIEW", "kind": kind,
            "source_layer": "client", "rule_ids": list(rule_ids), "blocking": False,
            "assessment_basis": "full_contract", "requires_word_comment": True,
            "detail": detail}


class ResolverError(Exception):
    """合成无法给出可信结果。调用方不得降级为部分结果。

    携带 `code`（冻结错误码目录中的值），使 Core、集成入口与 HTTP 三处
    对同一情形报**同一个码**——此前同一「active manifest 损坏」在
    三处分别是无码异常 / SNAPSHOT_INTEGRITY_FAILED / HUMAN_CONFIRMATION_REQUIRED
    （W7R3-SNAPSHOT-CORRUPT-CODES）。
    """

    def __init__(self, message: str, *, code: str = "SNAPSHOT_INTEGRITY_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Candidate:
    rule_id: str
    version: str
    source_layer: str
    source_snapshot: str
    normative_class: str
    content_hash: str
    payload: Mapping[str, Any]
    matched_scope: Mapping[str, Any]
    inclusion_reason: str
    overridden_rule_ids: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()


class Resolver:
    """在 PublicRepository 与 ClientRepository 之上实现 active 客户 overlay。"""

    def __init__(self, *, public_repository: Any, client_repository: Any) -> None:
        self._public = public_repository
        self._clients = client_repository

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def resolve_effective_rules(
        self,
        context: Mapping[str, Any],
        *,
        public_fingerprint_override: Optional[str] = None,
    ) -> models.EffectiveRuleSet:
        ctx = dict(context)
        try:
            clause_groups.validate_review_scope(ctx)
        except ValueError as exc:
            raise ResolverError(str(exc), code="INVALID_SCHEMA") from exc
        profile_id = ctx.get("client_profile_id")
        snapshot_id = ctx.get("client_policy_snapshot_id")

        public_result = self._public.resolve_public_rules(
            primary_type_id=ctx["primary_type_id"],
            secondary_type_ids=list(ctx.get("secondary_type_ids") or ()),
            our_role=ctx["our_role"],
            scene_tags=list(ctx.get("scene_tags") or ()),
            classification_status=ctx.get("classification_status", "high"),
            confirmed_domain_code=ctx.get("confirmed_domain_code", ""),
        )

        candidates: List[_Candidate] = [
            _Candidate(
                rule_id=r.rule_id, version=r.version, source_layer=r.source_layer,
                source_snapshot=r.source_snapshot, normative_class=r.normative_class,
                content_hash=r.content_hash, payload=dict(r.rule_payload or {}),
                matched_scope=dict(r.matched_scope), inclusion_reason=r.inclusion_reason,
                overridden_rule_ids=tuple(r.overridden_rule_ids),
                dependencies=tuple(r.dependencies),
            )
            for r in public_result.effective_rules
        ]
        excluded: List[Dict[str, Any]] = [dict(e) for e in public_result.excluded_rules]
        conflicts: List[Dict[str, Any]] = [dict(c) for c in public_result.rule_conflicts]
        human = public_result.human_confirmation_required
        overlay_active = False

        # ---- 客户 overlay ------------------------------------------------ #
        if profile_id is not None:
            fingerprint_expected = public_fingerprint_override or self._public.public_asset_fingerprint
            snapshot, overlay_active, overlay_conflicts, overlay_human = self._load_client_overlay(
                profile_id=profile_id,
                snapshot_id=snapshot_id,
                expected_fingerprint=fingerprint_expected,
            )
            conflicts.extend(overlay_conflicts)
            human = human or overlay_human

            if overlay_active and snapshot is not None:
                client_candidates, client_excluded, client_human, client_conflicts = (
                    self._build_client_candidates(ctx, snapshot)
                )
                candidates.extend(client_candidates)
                excluded.extend(client_excluded)
                conflicts.extend(client_conflicts)
                human = human or client_human

                # 本案授权：作用于**全量**候选（只影响被点名的规则），
                # 不得因此丢弃公共层候选。
                candidates, auth_conflicts, auth_human = self._apply_authorizations(
                    ctx, candidates
                )
                conflicts.extend(auth_conflicts)
                human = human or auth_human

        # ---- 硬边界（确定性 guard）-------------------------------------- #
        guard_verdict = guard_catalog.evaluate_hard_guards(
            guard_catalog.build_guard_catalog(self._public),
            rules=[c.payload for c in candidates if c.source_layer == "client"],
            facts=ctx.get("facts") or {},
            target_keys={
                (c.payload.get("effect") or {}).get("target_key")
                for c in candidates
                if isinstance(c.payload.get("effect"), Mapping)
                and (c.payload.get("effect") or {}).get("target_key")
            },
            context=ctx,
        )
        blocked = set(guard_verdict.blocked_rule_ids)
        if blocked:
            candidates = [c for c in candidates if c.rule_id not in blocked]
            for rule_id in sorted(blocked):
                excluded.append({
                    "rule_id": rule_id,
                    "reason": "hard_boundary_conflict: 触及全局硬边界，未执行并转人工",
                })
        conflicts.extend(dict(c) for c in guard_verdict.conflicts)
        human = human or guard_verdict.human_confirmation_required

        # ---- 依赖与冲突 -------------------------------------------------- #
        available_ids = {c.rule_id for c in candidates}
        dependency = guard_catalog.check_dependencies(
            [c.payload for c in candidates if c.source_layer == "client"],
            available_rule_ids=available_ids,
        )
        if not dependency.satisfied:
            dropped = [
                c for c in candidates
                if c.source_layer == "client"
                and any(d in dependency.missing for d in c.dependencies)
            ]
            keep = {c.rule_id for c in candidates} - {c.rule_id for c in dropped}
            candidates = [c for c in candidates if c.rule_id in keep]
            for c in dropped:
                excluded.append({
                    "rule_id": c.rule_id,
                    "reason": f"dependencies_missing: {sorted(set(c.dependencies) & set(dependency.missing))}",
                })
            if dropped and ctx.get("review_scope") == "clause_groups":
                # A narrow review may exclude a required companion topic. Do
                # not present the remaining subset as a complete clean review.
                human = True
                conflicts.append({
                    "code": "DEPENDENCY_UNSATISFIED", "kind": "review_scope_dependency_missing",
                    "rule_ids": [c.rule_id for c in dropped],
                    "missing_rule_ids": sorted(dependency.missing),
                    "detail": "指定条款类别审查所需的前置规则不可用；请核对依赖并明确扩大审查范围或修正规则。",
                })

        client_rule_payloads = [c.payload for c in candidates if c.source_layer == "client"]
        detected = guard_catalog.detect_conflicts(client_rule_payloads)
        if detected:
            for conflict in detected:
                # These are client-to-client business choices only. Hard guards
                # have already run over every potential client rule above.
                ids = list(conflict.get("rule_ids") or conflict.get("pair") or ())
                conflicts.append(_application_review(
                    str(conflict["kind"]), ids,
                    "客户规则存在不同商业口径；请结合合同全文判断可同时落实的部分，"
                    "对不能同时满足的选择在 Word 批注中说明，不伪造客户决定，继续其他修订。",
                    target_key=conflict.get("target_key"),
                    original_conflict=dict(conflict),
                ))

        # ---- 组装 -------------------------------------------------------- #
        # 结果中**不含客户层规则**时，必须保持公共快照自身的发射顺序，
        # 即 2.8.1 的 global → domain → principles → modules 目录序（RES-024 / INT-008）。
        # §7.6 的性质排序是"建议"，用于客户 overlay 混层时表达优先级；
        # 对纯公共结果套用它会让全局政策文档掉到末尾、模块被改成 rule_id
        # 字母序，与 2.8.1 不等价（W7 独立验证发现的 R0 缺陷）。
        # 中间各步（授权、硬边界、依赖剔除）均保序，故此处保留即为目录序。
        has_client_layer = any(c.source_layer == "client" for c in candidates)
        if has_client_layer:
            candidates.sort(key=self._sort_key)
        effective = [
            models.EffectiveRule(
                rule_id=c.rule_id, version=c.version, source_layer=c.source_layer,
                source_snapshot=c.source_snapshot, matched_scope=dict(c.matched_scope),
                inclusion_reason=c.inclusion_reason, normative_class=c.normative_class,
                content_hash=c.content_hash, overridden_rule_ids=c.overridden_rule_ids,
                dependencies=c.dependencies,
                payload_schema_id=c.payload.get("_payload_schema_id")
                or "one-contract/rule-graph-v1.schema.json",
                rule_payload=dict(c.payload),
            )
            for c in candidates
        ]
        trace = [
            models.ResolutionTraceItem(
                rule_id=c.rule_id, version=c.version, source_layer=c.source_layer,
                source_snapshot=c.source_snapshot, matched_scope=dict(c.matched_scope),
                decision="included", content_hash=c.content_hash,
                overridden_rule_ids=c.overridden_rule_ids, dependencies=c.dependencies,
            )
            for c in candidates
        ]

        # 记录**实际使用的**客户快照 ID，而不是调用方传入的原始值：
        # 未显式给快照时本方法会从 active manifest 隐式固定一个（INT-010），
        # 若此处回填原始入参，结果会变成"客户规则已生效但快照 ID 为 null"，
        # 溯源（RES-026 / INT-004 / INT-009）随之失效（生产者侧 HTTP 冒烟发现）。
        resolved_snapshot_id = None
        if overlay_active:
            resolved_snapshot_id = (snapshot or {}).get("snapshot_id") or snapshot_id

        result = models.EffectiveRuleSet(
            schema_version="1.0",
            public_snapshot_id=self._public.public_snapshot_id,
            context_hash=models.content_hash(
                {k: v for k, v in ctx.items() if k != "client_policy_snapshot_id"}
            ),
            client_profile_id=profile_id,
            client_policy_snapshot_id=resolved_snapshot_id,
            effective_rules=effective,
            excluded_rules=excluded,
            rule_conflicts=conflicts,
            coverage_level=public_result.coverage_level,
            coverage_notice=public_result.coverage_notice,
            resolution_trace=trace,
            human_confirmation_required=human,
        )
        result.client_overlay_active = overlay_active  # type: ignore[attr-defined]
        return result.with_hash()

    # ------------------------------------------------------------------ #
    # 客户 overlay
    # ------------------------------------------------------------------ #
    def _load_client_overlay(
        self,
        *,
        profile_id: str,
        snapshot_id: Optional[str],
        expected_fingerprint: str,
    ) -> Tuple[Optional[Mapping[str, Any]], bool, List[Dict[str, Any]], bool]:
        """返回 (snapshot, overlay_active, conflicts, human_confirmation_required)。"""
        try:
            profile = self._clients.get_client_profile(profile_id)
        except Exception as exc:
            return None, False, [{
                "code": "CLIENT_PROFILE_NOT_FOUND",
                "client_profile_id": profile_id,
                "detail": str(exc)[:200],
            }], True

        # DATA-005：非 active 的客户档案不得进入合成。
        # 写路径（client_repository）已有校验，但读/合成路径此前没有，
        # 导致 inactive 客户的规则仍会生效（W7 独立验证发现的 R0 缺陷）。
        # 错误码用冻结目录的 CANDIDATE_NOT_EXECUTABLE（非 active 资产试图生效），
        # 而不是 CLIENT_PROFILE_NOT_FOUND——档案确实存在，只是不可执行。
        if profile.get("status") != "active":
            return None, False, [{
                "code": "CANDIDATE_NOT_EXECUTABLE",
                "client_profile_id": profile_id,
                "profile_status": profile.get("status"),
                "detail": f"客户档案状态为 {profile.get('status')!r}，不参与合成；"
                          "请先恢复为 active，或明确改选仅公共规则。",
            }], True

        try:
            active = self._clients.load_active_snapshot(profile_id)
        except Exception as exc:
            # active manifest 损坏 → fail closed，**不回退**草稿或最新文件（RES-023）。
            # 这是不可恢复的存储损坏，直接抛出而不是返回“空 overlay”，
            # 否则调用方可能误以为“该客户没有规则”而继续出审查结论。
            raise ResolverError(
                f"客户 {profile_id} 的 active manifest 损坏，拒绝加载：{str(exc)[:180]}"
            ) from exc

        if active is None:
            return None, False, [{
                "code": "SNAPSHOT_INTEGRITY_FAILED",
                "client_profile_id": profile_id,
                "detail": "该客户尚无已发布的 active 快照；不使用最近历史快照（INT-010）。",
            }], True

        target_id = snapshot_id or active["snapshot_id"]
        try:
            snapshot = self._clients.load_snapshot(profile_id, target_id)
        except Exception as exc:
            return None, False, [{
                "code": "SNAPSHOT_INTEGRITY_FAILED",
                "client_profile_id": profile_id,
                "snapshot_id": target_id,
                "detail": str(exc)[:200],
            }], True

        bound = snapshot.get("public_asset_fingerprint")
        if bound != expected_fingerprint:
            # 公共升级 → 暂停 overlay，客户数据保留（DATA-006 / ADR-022）
            return None, False, [{
                "code": "PUBLIC_SNAPSHOT_MISMATCH",
                "client_profile_id": profile_id,
                "snapshot_id": target_id,
                "snapshot_public_fingerprint": bound,
                "current_public_fingerprint": expected_fingerprint,
                "detail": "客户快照绑定的公共指纹与当前公共版本不一致；"
                          "客户数据已保留但 overlay 已暂停。",
            }], True

        return snapshot, True, [], False

    def _build_client_candidates(
        self, ctx: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> Tuple[List[_Candidate], List[Dict[str, Any]], bool, List[Dict[str, Any]]]:
        try:
            rules = self._clients_snapshot_rules(snapshot)
        except Exception as exc:
            return [], [{"rule_id": "<snapshot>", "reason": f"snapshot_rules_unreadable: {exc}"}], True, []

        candidates: List[_Candidate] = []
        excluded: List[Dict[str, Any]] = []
        undetermined: List[Dict[str, Any]] = []
        human = False
        now = self._parse_time(ctx.get("now")) or datetime.now(timezone.utc)
        condition_catalog = guard_catalog.build_guard_catalog(self._public)

        for rule in rules:
            rule_id = str(rule.get("client_rule_id"))
            if not guard_catalog.is_rule_executable(rule):
                excluded.append({
                    "rule_id": rule_id,
                    "reason": "not_executable: 审批轴与执行轴必须同为 active（RES-025）",
                })
                continue

            validity = rule.get("validity") or {}
            start = self._parse_time(validity.get("valid_from"))
            end = self._parse_time(validity.get("valid_until"))
            if start and now < start:
                excluded.append({"rule_id": rule_id, "reason": "not_yet_valid: 尚未生效"})
                continue
            if end and now > end:
                excluded.append({"rule_id": rule_id, "reason": "expired: 已过期"})
                continue

            mismatch_reason, undetermined_reason = self._scope_mismatch(rule, ctx)
            # Explicitly selected review boundaries and invalid catalog values
            # remain gates. Business dimensions are hints for full-text review.
            if mismatch_reason and mismatch_reason.startswith("scope_mismatch[clause_groups]"):
                excluded.append({"rule_id": rule_id, "reason": mismatch_reason})
                continue
            if undetermined_reason and undetermined_reason.startswith("scope_undetermined[clause_groups]"):
                human = True
                excluded.append({"rule_id": rule_id, "reason": undetermined_reason})
                undetermined.append({"code": "INVALID_SCHEMA", "kind": "invalid_client_scope",
                                     "rule_ids": [rule_id], "detail": undetermined_reason})
                continue
            scope_reason = mismatch_reason or undetermined_reason
            if scope_reason:
                undetermined.append(_application_review(
                    "scope_mismatch" if mismatch_reason else "scope_undetermined", [rule_id],
                    "结构化适用范围预筛仅供参考；请结合合同全文判断实际适用性，"
                    "并在 Word 批注中说明判断与事实不足。", prefilter_reason=scope_reason,
                ))

            condition_state, condition_issues = guard_catalog.evaluate_client_condition(
                condition_catalog, rule, ctx.get("facts") or {}
            )
            if condition_issues:
                # An invalid rule/fact declaration is not a semantic ambiguity.
                # It must be repaired, not presented as safe AI discretion.
                human = True
                excluded.append({"rule_id": rule_id, "reason": "condition_invalid: 客户条件或事实结构无效"})
                undetermined.append({"code": "INVALID_SCHEMA", "kind": "invalid_client_condition",
                                     "rule_ids": [rule_id], "issues": condition_issues,
                                     "detail": "客户条件或事实声明未通过结构校验，须修正后使用。"})
                continue
            if condition_state != "matched":
                kind = {"semantic": "semantic_condition", "unknown": "condition_undetermined",
                        "unmatched": "condition_unmatched"}[condition_state]
                undetermined.append(_application_review(
                    kind, [rule_id], "请依据合同全文评估客户条件；结构化预筛结果不等于最终适用结论。"
                    "事实不足或涉及商业选择时保留明确 Word 批注，继续其余可落实修订。",
                    prefilter_state=condition_state,
                    natural_language_note=(rule.get("condition") or {}).get("natural_language_note", ""),
                ))
            if (rule.get("condition") or {}).get("human_confirmation_required"):
                undetermined.append(_application_review(
                    "rule_confirmation", [rule_id],
                    "旧规则的人工确认标记作为审查提示保留，不作为开始审查或交付的前置。"
                    "AI 应核对全文，并在 Word 批注中说明判断、待核实事实或商业选择。",
                    legacy_human_confirmation_required=True,
                ))
            payload = dict(rule)
            candidates.append(_Candidate(
                rule_id=rule_id,
                version=str(rule.get("version") or "0.0.0"),
                source_layer="client",
                source_snapshot=str(snapshot.get("snapshot_id")),
                normative_class=models.normative_class_for_client_level(
                    str(rule.get("normative_level") or "advisory")
                ),
                content_hash=str(rule.get("content_sha256") or ""),
                payload=payload,
                matched_scope=dict(rule.get("scope") or {}),
                inclusion_reason=self._client_inclusion_reason(rule, ctx, condition_state, scope_reason),
                dependencies=tuple(rule.get("depends_on") or ()),
            ))

        return candidates, excluded, human, undetermined

    def _clients_snapshot_rules(self, snapshot: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        """读取已发布快照内的规则集合。

        经 `publisher.Publisher.load_snapshot` 读取：它会**复核内容哈希**，
        篡改过的快照会抛错而不是被静默使用（LIFE-008）。W3A 未对外暴露
        snapshot→rules 的读取方法，且 W3A 模块不在本包白名单内，故在此复用
        publisher 的完整性校验路径，不新增对客户存储的旁路。
        """
        from . import paths, publisher

        profile_dir = paths.contained_path(
            self._clients.clients_root, snapshot["client_profile_id"]
        )
        doc = publisher.Publisher(profile_dir).load_snapshot(snapshot["snapshot_id"])
        return tuple(doc["rules_doc"].get("rules", ()))

    @staticmethod
    def _client_inclusion_reason(rule: Mapping[str, Any], ctx: Mapping[str, Any],
                                 condition_state: str = "matched", scope_reason: Optional[str] = None) -> str:
        groups = (rule.get("scope") or {}).get("clause_groups") or []
        reason = "已启用的客户规则进入 AI 合同全文审查集合，不表示已决定采用"
        if scope_reason:
            reason += "；范围预筛：" + scope_reason
        if condition_state == "semantic":
            reason += "；自然语言条件须结合合同全文判断，空结构化子句不表示无条件适用"
        else:
            reason += "；结构化条件预筛=" + condition_state
        if groups:
            labels = "、".join(clause_groups.CLAUSE_GROUP_LABELS[group] for group in groups)
            reason += "；仅作用于“" + labels + "”条款，实际落实须核对条款归属，缺失条款可补写"
            if ctx.get("review_scope", "whole_contract") == "whole_contract":
                reason += "；整合同审查保留此要求，不据现有章节是否存在排除"
            else:
                reason += "；与本次明确选择的条款类别有交集，实际处理仅限交集"
        else:
            reason += "；规则未限定条款类别"
            if ctx.get("review_scope") == "clause_groups":
                reason += "，实际处理仍限于本次明确选择的类别"
        return reason

    def _scope_mismatch(
        self, rule: Mapping[str, Any], ctx: Mapping[str, Any]
    ) -> Tuple[Optional[str], Optional[str]]:
        """返回 `(mismatch_reason, undetermined_reason)`，二者互斥。

        **「上下文未提供该维度」不得当作「取值不匹配」。** 原实现用
        `if allowed and value not in allowed`，缺维度时 `value` 为 `None`，
        `None not in allowed` 成立 → 规则被静默丢弃，且无冲突、无转人工，
        结果看起来像一份干净的纯公共结果（第三轮独立验证确认的 R0）。

        缺维度与业务不匹配均保留为 AI 全文审查的预筛信息，不能静默当作
        已适用或直接丢弃；明确限定的审查类别仍是本次任务边界。
        """
        scope = rule.get("scope") or {}
        try:
            allowed_groups = clause_groups.validate_clause_groups(
                scope.get("clause_groups", []), field="scope.clause_groups")
            mode, selected_groups = clause_groups.validate_review_scope(ctx)
        except ValueError as exc:
            return None, "scope_undetermined[clause_groups]: " + str(exc)
        if scope.get("applies_to_all") and allowed_groups:
            return None, "scope_undetermined[clause_groups]: 全部适用与条款类别限制矛盾，须先修正规则"
        if scope.get("applies_to_all"):
            return None, None

        # Check the user's explicit task boundary before business dimensions;
        # a role mismatch must not hide a disjoint selected review scope.
        if mode == "clause_groups" and allowed_groups and not set(allowed_groups).intersection(selected_groups):
            return ("scope_mismatch[clause_groups]: 规则限定 " + str(sorted(allowed_groups))
                    + "，本次明确审查类别 " + str(sorted(selected_groups)) + " 与其无交集", None)

        undetermined: List[str] = []
        checks = (
            ("domain_codes", ctx.get("primary_domain_code"), "domain"),
            ("type_ids", ctx.get("primary_type_id"), "type"),
            ("roles", ctx.get("our_role"), "role"),
            ("contract_stages", ctx.get("contract_stage"), "stage"),
        )
        for key, value, label in checks:
            allowed = scope.get(key) or ()
            if not allowed:
                continue
            if value is None:
                undetermined.append(f"{label}（规则限定 {sorted(allowed)}）")
                continue
            if value not in allowed:
                return (f"scope_mismatch[{label}]: 规则限定 {sorted(allowed)}，本案为 {value!r}",
                        None)

        allowed_scenes = set(scope.get("scene_tags") or ())
        scenes = set(ctx.get("scene_tags") or ())
        if allowed_scenes:
            if not scenes:
                undetermined.append(f"scene（规则限定 {sorted(allowed_scenes)}）")
            elif not (allowed_scenes & scenes):
                return (f"scope_mismatch[scene]: 规则限定 {sorted(allowed_scenes)}，本案场景无交集",
                        None)

        if undetermined:
            return None, (
                "scope_undetermined: 上下文未提供 " + "、".join(undetermined)
                + "；留待 AI 依据合同全文判断，不预先视为适用或不适用"
            )
        return None, None

    # ------------------------------------------------------------------ #
    # 本案授权
    # ------------------------------------------------------------------ #
    def _apply_authorizations(
        self, ctx: Mapping[str, Any], candidates: List[_Candidate]
    ) -> Tuple[List[_Candidate], List[Dict[str, Any]], bool]:
        """本案授权例外。

        有有效授权：被点名规则**保留在结果中但标记为被覆盖**，并记录授权人、
        时间、理由与被覆盖规则 ID（RES-018）。不 Physical 删除候选，
        以便 trace 与审计都能看到"它被覆盖了"这一事实。

        无有效授权：不静默覆盖。纯客户商业选择交 AI 留批注；跨客户、公共规则
        或未知目标的授权请求保留阻断，不能借业务提示绕过身份和法律边界。
        """
        authorizations = list(ctx.get("case_authorizations") or ())
        instruction = ctx.get("case_instruction") or {}
        target_ids = set(instruction.get("overrides_rule_ids") or ())

        # 未声明任何本案指令或授权 → 无豁免，原样返回
        if not target_ids and not authorizations:
            return candidates, [], False

        # 提供了授权声明但未点名具体规则时，以授权声明的规则集合为准。
        # 这是保守语义：授权声明本身就是"请求豁免"的表示，必须被校验，
        # 不能因为缺少 case_instruction 就静默忽略（过期授权尤其如此）。
        if not target_ids:
            for auth in authorizations:
                target_ids |= set(auth.get("overrides_rule_ids") or ())
            if not target_ids:
                return candidates, [], False

        conflicts: List[Dict[str, Any]] = []
        now = self._parse_time(ctx.get("now")) or datetime.now(timezone.utc)

        # 授权必须**属于本案客户**（`case-authorization-v1` 把 client_profile_id
        # 列为必填；SEC-003 要求两客户严格隔离）。原实现只读 overrides_rule_ids
        # 等少数字段、从不校验归属——实测「归属 A 的授权」可覆盖 B 的规则，
        # 跨客户隔离在授权路径上是破的。缺归属或归属不符一律 fail closed。
        expected_profile = ctx.get("client_profile_id")
        scope_rejected: List[Mapping[str, Any]] = []
        valid_auth = None
        for auth in authorizations:
            expires = self._parse_time(auth.get("expires_at"))
            if expires and now > expires:
                continue
            if not target_ids.issubset(set(auth.get("overrides_rule_ids") or ())):
                continue
            if expected_profile is None or auth.get("client_profile_id") != expected_profile:
                scope_rejected.append(auth)
                continue
            valid_auth = auth
            break

        if valid_auth is None:
            if scope_rejected:
                conflicts.append({
                    "code": "HUMAN_CONFIRMATION_REQUIRED",
                    "kind": "authorization_scope_mismatch",
                    "overridden_rule_ids": sorted(target_ids),
                    "client_profile_id": expected_profile,
                    "authorization_profile_ids": sorted(
                        str(a.get("client_profile_id")) for a in scope_rejected),
                    "detail": "提供的授权不属于本案客户（或未声明归属），"
                              "不得用于覆盖本案规则；请改用本客户的授权，"
                              "或由律师另行确认。不静默生效。",
                })
                return candidates, conflicts, True
            conflict = {
                "code": "HUMAN_CONFIRMATION_REQUIRED",
                "kind": "authorization_missing",
                "overridden_rule_ids": sorted(target_ids),
                "detail": "本案指令要求突破客户强制政策，但缺少有效授权；不静默覆盖。",
            }
            known_client_ids = {c.rule_id for c in candidates if c.source_layer == "client"}
            if target_ids.issubset(known_client_ids):
                conflicts.append(_application_review(
                    "authorization_missing", sorted(target_ids),
                    "本案指令与客户规则存在商业选择且未见有效授权。保留原规则，不声称例外已获授权；"
                    "由 AI 结合合同全文评估，在 Word 批注中明确待客户决定的选择并继续其他修订。",
                    overridden_rule_ids=[], requested_override_rule_ids=sorted(target_ids),
                ))
                return candidates, conflicts, False
            conflicts.append(conflict)
            return candidates, conflicts, True

        out: List[_Candidate] = []
        for candidate in candidates:
            if candidate.rule_id in target_ids:
                conflict = {
                    "code": "HUMAN_CONFIRMATION_REQUIRED",
                    "kind": "case_authorized_override",
                    "overridden_rule_ids": [candidate.rule_id],
                    # 必须记录**是哪一份授权**：此前只记授权人/时间/理由，
                    # 审计无法引用具体的授权记录（`case-authorization-v1`
                    # 的 authorization_id 与 evidence_refs 均未被读取）。
                    "authorization_id": valid_auth.get("authorization_id"),
                    "authorized_by": valid_auth.get("authorized_by"),
                    "authorized_at": valid_auth.get("authorized_at"),
                    "reason": valid_auth.get("reason"),
                    "authentication_level": valid_auth.get("authentication_level"),
                    "evidence_refs": list(valid_auth.get("evidence_refs") or ()),
                }
                if candidate.source_layer == "client":
                    conflict = _application_review(
                        "case_authorized_override", [candidate.rule_id],
                        "已记录本案客户授权例外；仍须依据合同全文评估如何落实，并在 Word 批注中说明。",
                        **{k: v for k, v in conflict.items() if k not in ("code", "kind")},
                    )
                conflicts.append(conflict)
                out.append(_Candidate(
                    rule_id=candidate.rule_id, version=candidate.version,
                    source_layer=candidate.source_layer,
                    source_snapshot=candidate.source_snapshot,
                    normative_class=candidate.normative_class,
                    content_hash=candidate.content_hash, payload=candidate.payload,
                    matched_scope=candidate.matched_scope,
                    inclusion_reason=f"被本案授权例外覆盖（授权人 {valid_auth.get('authorized_by')}）",
                    overridden_rule_ids=tuple(
                        dict.fromkeys(candidate.overridden_rule_ids + (candidate.rule_id,))
                    ),
                    dependencies=candidate.dependencies,
                ))
                continue
            out.append(candidate)
        return out, conflicts, False

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _sort_key(c: _Candidate) -> Tuple[int, int, int, str, str]:
        """稳定排序：性质 → 来源专指度 → 显式优先级 → 稳定 ID → 版本。

        禁止依赖文件系统遍历顺序、JSON 对象顺序或写入时间（§7.6）。
        """
        class_rank = _CLASS_ORDER.index(c.normative_class) if c.normative_class in _CLASS_ORDER else 99
        layer_rank = _LAYER_ORDER.index(c.source_layer) if c.source_layer in _LAYER_ORDER else 99
        priority = int(c.payload.get("priority_within_class") or 0)
        # 来源越具体排名越靠前：client > type > domain > global
        specificity = -_LAYER_ORDER.index(c.source_layer) if c.source_layer in _LAYER_ORDER else 0
        return (class_rank, specificity, priority, c.rule_id, c.version)

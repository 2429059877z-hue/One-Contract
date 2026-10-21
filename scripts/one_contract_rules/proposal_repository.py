#!/usr/bin/env python3
"""公共规则的提案式维护（proposal store）。

契约来源：ADR-003（公共修改采用 proposal）、ADR-002（公共 active 只读）、
architecture-contract.md §4（proposal 路由与错误码）、RS-HC-001；
public-rule-proposal-v1.schema.json（冻结契约）。

硬边界（本模块的核心价值就在这些"不做"上）：
- **绝不原地修改公共 active 资产**：proposal 是独立工作区的独立文件；
- **绝不进入 runtime**：resolver 与 PublicRepository 都不引用本模块；
- **没有跳过审批直接激活的 API**：不存在 activate/apply/publish/promote 方法；
- 即使 proposal 走到 `accepted`，也只是一种记录状态，公共资产仍然零改动
  ——正式晋级属知识治理流程（2.9.x），不在本包。
"""
from __future__ import annotations

import json
import copy
import os
import shutil
import uuid
import re
from functools import wraps
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import models, paths, registry, private_fs, proposal_validation
from .transaction_lock import profile_lock

__all__ = [
    "NEXT_STATUS",
    "PROPOSAL_STATUSES",
    "ProposalError",
    "ProposalRepository",
]

#: 提案状态机。只允许逐级向前或转 rejected（终态）。
PROPOSAL_STATUSES: Tuple[str, ...] = (
    "candidate", "lawyer_approved", "regression_passed", "accepted", "rejected", "deprecated",
)

NEXT_STATUS: Mapping[str, Tuple[str, ...]] = {
    "candidate": ("rejected",),
    "lawyer_approved": ("accepted", "rejected"),
    "regression_passed": ("accepted", "rejected"),
    "accepted": (),
    "rejected": (),
    "deprecated": (),
}

_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: 晋级链路各阶段需跑的回归项（PROP-006 要求导出含测试清单）。
_REGRESSION_CHECKLIST: Tuple[Mapping[str, str], ...] = (
    {"check_id": "public_hash", "description": "公共资产前后 SHA-256 必须一致"},
    {"check_id": "selector_equivalence", "description": "与 2.8.1 选择器结果逐条等价"},
    {"check_id": "resolver_golden", "description": "解析 golden 用例全通过"},
    {"check_id": "hard_boundary", "description": "硬边界用例未被绕过"},
    {"check_id": "export_privacy", "description": "公开包隐私扫描零命中"},
)


class ProposalError(Exception):
    """提案操作失败。调用方不得降级为部分写入。"""


class ProposalConflict(ProposalError):
    """Caller is acting on an obsolete proposal version."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_hash(payload: Mapping[str, Any]) -> Dict[str, Any]:
    body = {k: v for k, v in payload.items() if k != "content_sha256"}
    body["content_sha256"] = models.content_hash(body)
    return body


def _secure_write(path: Path, payload: Mapping[str, Any]) -> None:
    private_fs.atomic_write_json(path, payload)


def _mutation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if self.read_only:
            raise ProposalError("当前工作台为只读模式。")
        with profile_lock(self.root):
            return method(self, *args, **kwargs)
    return guarded


@dataclass
class ProposalRepository:
    """外部 data root 上的提案仓库。

    注意：本类**故意不提供**任何 activate / apply / publish / promote 方法。
    晋级到公共资产属知识治理流程，不在 Rule Studio 内。
    """

    data_root: Path
    public_repository: Any
    read_only: bool = False

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root).expanduser().resolve()
        self.root = self.data_root / "public-proposals"
        if not self.read_only:
            private_fs.secure_dir(self.root)

    # ------------------------------------------------------------------ #
    # 创建
    # ------------------------------------------------------------------ #
    @_mutation
    def create_proposal(
        self,
        *,
        operation: str,
        layer: str,
        target_rule_id: Optional[str],
        title: str,
        rationale: str,
        source_refs: Sequence[str],
        proposed_rule: Optional[Mapping[str, Any]],
        actor: str,
        new_proposal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if operation not in ("create", "supersede", "deprecate"):
            raise ProposalError(f"未支持的提案操作: {operation!r}")
        if new_proposal_id is not None:
            if not re.fullmatch(r"pr_[a-f0-9]{12}", new_proposal_id) or self._proposal_path(new_proposal_id).exists():
                raise ProposalError("新建议编号无效或已被使用。")
        if layer not in ("global", "domain", "type"):
            raise ProposalError(f"未支持的层级: {layer!r}")
        if not title or not rationale:
            raise ProposalError("提案必须包含 title 与 rationale。")
        if proposal_validation.current_fingerprint(self.public_repository) != self.public_repository.public_asset_fingerprint:
            raise ProposalError("公共规则已变化，请重启工作台载入最新基线后再创建建议。")

        # create 不得指向既有规则；其余操作必须指向既有 active 规则
        if operation == "create":
            if target_rule_id:
                raise ProposalError("create 提案不得携带 target_rule_id。")
        else:
            if not target_rule_id:
                raise ProposalError(f"{operation} 提案必须指定 target_rule_id。")
            if self.public_repository.get_rule(target_rule_id) is None:
                raise ProposalError(
                    f"目标规则不存在于公共知识: {target_rule_id!r}；"
                    "不得对不存在的规则提出修改。"
                )

        proposal = {
            "schema_version": "1.0",
            "proposal_id": new_proposal_id or f"pr_{uuid.uuid4().hex[:12]}",
            "operation": operation,
            "layer": layer,
            "target_rule_id": target_rule_id,
            "base_public_fingerprint": self.public_repository.public_asset_fingerprint,
            "title": title,
            "rationale": rationale,
            "source_refs": list(source_refs),
            "proposed_rule": copy.deepcopy(dict(proposed_rule)) if proposed_rule is not None else None,
            "impact": {
                # 契约要求四个必填字段；human_review_required 恒为 true
                # ——公共规则晋级必须经人工复核，不得自动化。
                "affected_rule_ids": [target_rule_id] if target_rule_id else [],
                "affected_type_ids": self._affected_type_ids(target_rule_id),
                "required_regression_ids": [c["check_id"] for c in _REGRESSION_CHECKLIST],
                "human_review_required": True,
                "impact_note": f"{operation} 提案；运行时不生效，须经知识治理流程晋级。",
            },
            "status": "candidate",
            "created_at": _now(),
            "created_by": actor,
            "content_sha256": "sha256:" + "0" * 64,
        }
        stored = _with_hash(proposal)
        self._validate(stored)
        self._write(stored)
        self._audit(stored["proposal_id"], "proposal_created", actor,
                    detail={"operation": operation, "layer": layer})
        return stored

    @_mutation
    def save_proposal(self, proposal_id: str, *, proposed_rule: Optional[Mapping[str, Any]],
                      title: str, rationale: str, source_refs: Sequence[str],
                      expected_content_sha256: str, actor: str) -> Dict[str, Any]:
        """Replace the complete candidate, retaining prior content and evidence.

        Incomplete work may remain a candidate. No merge supplies missing legal
        content, and its executed validation cannot pass until all gaps are fixed.
        """
        proposal = self.get_proposal(proposal_id)
        self._check_expected(proposal, expected_content_sha256)
        if proposal["status"] in ("accepted", "rejected", "deprecated"):
            raise ProposalError("已结束的建议保留原记录；请另建新的修改建议。")
        updated = {**proposal, "title": title, "rationale": rationale, "source_refs": list(source_refs),
                   "proposed_rule": copy.deepcopy(dict(proposed_rule)) if proposed_rule is not None else None}
        if proposal_validation.subject_hash(updated) != proposal_validation.subject_hash(proposal):
            updated["status"] = "candidate"
        stored = _with_hash(updated)
        self._validate(stored)
        history = paths.contained_path(self.root, proposal_id, "revisions", proposal["content_sha256"].split(":")[1] + ".json")
        if not history.exists():
            _secure_write(history, proposal)
        self._write(stored)
        self._audit(proposal_id, "proposal_saved", actor,
                    detail={"before_sha256": proposal["content_sha256"], "after_sha256": stored["content_sha256"]})
        return stored

    @staticmethod
    def _check_expected(proposal, expected):
        if not isinstance(expected, str) or expected != proposal["content_sha256"]:
            raise ProposalConflict("建议已变化，请重新载入并核对具体内容（PROPOSAL_CONTENT_CONFLICT）。")

    def _affected_type_ids(self, target_rule_id: Optional[str]) -> List[str]:
        """受影响的具体类型。委派给公共索引，不在本模块推导规则语义。"""
        if not target_rule_id:
            return []
        node = self.public_repository.get_rule(target_rule_id)
        if node is None:
            return []
        return sorted(set(node.type_ids))

    def _validate(self, proposal: Mapping[str, Any]) -> None:
        validator = registry.make_validator(
            "one-contract/public-rule-proposal-v1.schema.json"
        )
        errors = sorted(validator.iter_errors(proposal), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            raise ProposalError(
                f"提案不符合公共契约: {'.'.join(str(p) for p in first.path)}: {first.message}"
            )

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def _proposal_path(self, proposal_id: str) -> Path:
        if not isinstance(proposal_id, str) or not re.fullmatch(r"pr_[a-z0-9][a-z0-9_-]{7,79}", proposal_id):
            raise ProposalError("提案编号无效。")
        try:
            return paths.contained_path(self.root, f"{proposal_id}.json")
        except paths.PathContractError as exc:
            raise ProposalError(str(exc)) from exc

    def _write(self, proposal: Mapping[str, Any]) -> None:
        _secure_write(self._proposal_path(str(proposal["proposal_id"])), proposal)

    def get_proposal(self, proposal_id: str) -> Dict[str, Any]:
        path = self._proposal_path(proposal_id)
        if not path.is_file():
            raise ProposalError(f"提案不存在: {proposal_id}")
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
            if proposal.get("proposal_id") != proposal_id or proposal.get("content_sha256") != _with_hash(proposal)["content_sha256"]:
                raise ProposalError("提案内容完整性校验失败。")
            self._validate(proposal)
            return proposal
        except json.JSONDecodeError as exc:
            raise ProposalError(f"提案文件损坏: {proposal_id}") from exc

    def list_proposals(
        self, *, status: Optional[str] = None, layer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("pr_*.json")):
            item = self.get_proposal(path.stem)
            if status and item.get("status") != status:
                continue
            if layer and item.get("layer") != layer:
                continue
            out.append(item)
        return out

    # ------------------------------------------------------------------ #
    # 状态机
    # ------------------------------------------------------------------ #
    @_mutation
    def transition(self, proposal_id: str, *, to_status: str, actor: str,
                   note: Optional[str] = None, expected_content_sha256: Optional[str] = None) -> Dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        current = proposal.get("status")
        if to_status == "lawyer_approved":
            raise ProposalError("请通过人工确认入口，对当前内容哈希作明确确认；不能只声明批准状态。")
        if to_status == "regression_passed":
            raise ProposalError("当前仅执行结构、引用和依赖检查，未执行公共解析/合同交付回归，不能声明 regression_passed。")
        if to_status not in PROPOSAL_STATUSES:
            raise ProposalError(f"未登记的提案状态: {to_status!r}")
        allowed = NEXT_STATUS.get(current, ())
        if to_status not in allowed:
            raise ProposalError(
                f"非法状态迁移：{current!r} → {to_status!r}；"
                f"允许的目标状态: {list(allowed) or '无（终态）'}。"
            )
        if to_status == "accepted":
            self._check_expected(proposal, expected_content_sha256)
            governance = self.get_governance(proposal_id)
            if not governance["ready_for_governance_export"]:
                raise ProposalError("须先完成当前候选内容的实际检查与人工确认，才能同意导出建议。")
        updated = dict(proposal)
        updated["status"] = to_status
        stored = _with_hash(updated)
        self._validate(stored)
        self._write(stored)
        self._audit(proposal_id, "proposal_transitioned", actor,
                    detail={"from": current, "to": to_status, "note": note})
        return stored

    # ------------------------------------------------------------------ #
    # 实际检查与人工确认；不可变证据独立于冻结的提案正文契约
    # ------------------------------------------------------------------ #
    def _evidence_records(self, proposal_id):
        directory = paths.contained_path(self.root, proposal_id, "evidence")
        if not directory.exists():
            return []
        records = []
        for path in sorted(directory.glob("pe_*.json")):
            if not re.fullmatch(r"pe_[a-f0-9]{32}", path.stem):
                raise ProposalError("建议证据编号无效。")
            safe = paths.contained_path(self.root, proposal_id, "evidence", path.name)
            try:
                record = json.loads(safe.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProposalError("建议证据无法读取，拒绝忽略缺失记录。") from exc
            if (not isinstance(record, dict) or record.get("content_sha256") != models.content_hash(record)
                    or record.get("proposal_id") != proposal_id or record.get("evidence_id") != path.stem
                    or record.get("kind") not in ("executed_validation", "human_confirmation")
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(record.get("subject_sha256", "")))
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(record.get("public_asset_fingerprint", "")))
                    or type(record.get("sequence")) is not int or record["sequence"] < 1):
                raise ProposalError("建议证据完整性或归属校验失败。")
            records.append(record)
        if len({r["sequence"] for r in records}) != len(records):
            raise ProposalError("建议证据序号冲突。")
        return sorted(records, key=lambda r: r["sequence"])

    def _write_evidence(self, proposal, *, kind, actor, report=None, validation=None, note=None):
        if not isinstance(actor, str) or not actor.strip():
            raise ProposalError("须记录实际操作者。")
        records = self._evidence_records(proposal["proposal_id"])
        record = {"evidence_id": "pe_" + uuid.uuid4().hex, "kind": kind,
            "proposal_id": proposal["proposal_id"], "subject_sha256": proposal_validation.subject_hash(proposal),
            "public_asset_fingerprint": proposal["base_public_fingerprint"],
            "sequence": max((r["sequence"] for r in records), default=0) + 1,
            "actor": actor, "recorded_at": _now(), "report": copy.deepcopy(report),
            "validation_evidence_id": validation["evidence_id"] if validation else None,
            "validation_sha256": validation["content_sha256"] if validation else None,
            "confirmation": True if kind == "human_confirmation" else None, "note": note,
            "effective": False}
        record = _with_hash(record)
        path = paths.contained_path(self.root, proposal["proposal_id"], "evidence", record["evidence_id"] + ".json")
        if path.exists():
            raise ProposalError("证据编号重复，禁止覆盖。")
        _secure_write(path, record)
        return record

    @_mutation
    def validate_proposal(self, proposal_id: str, *, expected_content_sha256: str, actor: str) -> Dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        self._check_expected(proposal, expected_content_sha256)
        report = proposal_validation.execute(proposal, self.public_repository)
        evidence = self._write_evidence(proposal, kind="executed_validation", actor=actor, report=report)
        self._audit(proposal_id, "proposal_validated", actor,
            detail={"evidence_id": evidence["evidence_id"], "subject_sha256": report["subject_sha256"], "passed": report["passed"]})
        return {"proposal": proposal, "validation": report, "evidence_id": evidence["evidence_id"],
                "governance": self.get_governance(proposal_id), "effective": False}

    def get_governance(self, proposal_id: str) -> Dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        subject = proposal_validation.subject_hash(proposal)
        records = self._evidence_records(proposal_id)
        matching = [r for r in records if r["subject_sha256"] == subject]
        validations = [r for r in matching if r["kind"] == "executed_validation"]
        confirmations = [r for r in matching if r["kind"] == "human_confirmation"]
        validation = validations[-1] if validations else None
        confirmation = confirmations[-1] if confirmations else None
        current = proposal_validation.current_fingerprint(self.public_repository)
        report = validation.get("report") if validation else None
        checks = report.get("checks") if isinstance(report, Mapping) else None
        validation_valid = bool(validation and isinstance(checks, list) and checks
            and all(isinstance(c, Mapping) for c in checks)
            and {c.get("check_id") for c in checks} == {"public_baseline", "complete_content", "schema", "references", "dependencies"}
            and report.get("passed") is True and all(c.get("passed") is True and c.get("issues") == [] for c in checks)
            and report.get("subject_sha256") == subject
            and report.get("public_asset_fingerprint") == current == proposal["base_public_fingerprint"]
            and report.get("validator") == {"name": "one-contract-public-proposal-checks", "version": proposal_validation.VALIDATOR_VERSION}
            and report.get("scope") == "public_proposal_structure_references_dependencies"
            and report.get("candidate_sha256") == models.content_hash({"candidate": proposal.get("proposed_rule")}, exclude=()))
        approval_valid = bool(validation_valid and confirmation and confirmation.get("confirmation") is True
            and confirmation.get("validation_evidence_id") == validation["evidence_id"]
            and confirmation.get("validation_sha256") == validation["content_sha256"]
            and confirmation.get("public_asset_fingerprint") == current)
        gaps = []
        if not validation_valid:
            gaps.append("当前内容尚无有效的正文、结构、引用和依赖检查记录。")
        if not approval_valid:
            gaps.append("当前内容及检查结果尚未获人工确认。")
        if current != proposal["base_public_fingerprint"]:
            gaps.append("公共基线已变化；须重新核对后创建新建议。")
        ready = validation_valid and approval_valid and proposal["status"] in ("lawyer_approved", "accepted")
        return {"subject_sha256": subject, "validation_valid": validation_valid, "human_confirmation_valid": approval_valid,
            "validation_evidence": validation, "human_confirmation_evidence": confirmation,
            "ready_for_governance_export": ready, "gaps": gaps,
            "public_regression_executed": False, "ready_for_public_release": False,
            "effective": False, "status_label": "同意导出建议，未启用" if proposal["status"] == "accepted" else "候选建议，未启用"}

    @_mutation
    def approve_proposal(self, proposal_id: str, *, expected_content_sha256: str,
                         confirmation: bool, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        self._check_expected(proposal, expected_content_sha256)
        if confirmation is not True:
            raise ProposalError("须明确确认已核对当前完整内容及实际检查结果。")
        if proposal["status"] not in ("candidate", "lawyer_approved", "regression_passed"):
            raise ProposalError("该建议已结束，不能再确认。")
        governance = self.get_governance(proposal_id)
        if not governance["validation_valid"]:
            raise ProposalError("正文缺项、断链或依赖问题尚未消除；请先对当前内容执行检查。")
        evidence = self._write_evidence(proposal, kind="human_confirmation", actor=actor,
            validation=governance["validation_evidence"], note=note)
        stored = _with_hash({**proposal, "status": "lawyer_approved"})
        self._write(stored)
        self._audit(proposal_id, "proposal_human_confirmed", actor,
            detail={"evidence_id": evidence["evidence_id"], "subject_sha256": governance["subject_sha256"]})
        return {"proposal": stored, "evidence_id": evidence["evidence_id"],
                "governance": self.get_governance(proposal_id), "effective": False}

    # ------------------------------------------------------------------ #
    # 差异
    # ------------------------------------------------------------------ #
    def proposal_diff(
        self,
        *,
        operation: str,
        layer: str,
        target_rule_id: Optional[str],
        proposed_rule: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        current = None
        current_body = None
        if target_rule_id:
            node = self.public_repository.get_rule(target_rule_id)
            if node is None:
                raise ProposalError(f"目标规则不存在: {target_rule_id!r}")
            current = {
                "node_id": node.node_id,
                "node_kind": node.node_kind,
                "layer": node.layer,
                "title": node.title,
                "version": node.version,
                "approval_status": node.approval_status,
                "activation_status": node.activation_status,
                "content_hash": node.content_hash,
            }
        else:
            current = None

        if target_rule_id:
            detail = self.public_repository.get_rule_detail(target_rule_id, projection_mode="maintenance")
            current_body = detail.get("body")
        proposed = dict(proposed_rule) if proposed_rule is not None else None

        changed: List[str] = []
        if proposed and current:
            comparison = current_body if isinstance(current_body, Mapping) else current
            for key in sorted(set(comparison) | set(proposed)):
                if comparison.get(key) != proposed.get(key) or (key in comparison) != (key in proposed):
                    changed.append(key)
        elif proposed and not current:
            changed = sorted(proposed.keys())

        return {
            "operation": operation,
            "layer": layer,
            "target_rule_id": target_rule_id,
            "current": current,
            "current_body": current_body,
            "proposed": proposed,
            "changed_fields": sorted(changed),
            "removes_current": operation == "deprecate",
            "effective": False,
        }

    # ------------------------------------------------------------------ #
    # 治理导出
    # ------------------------------------------------------------------ #
    def export_governance_bundle(self, proposal_id: str) -> Dict[str, Any]:
        proposal = self.get_proposal(proposal_id)
        governance = self.get_governance(proposal_id)
        diff = self.proposal_diff(
            operation=str(proposal["operation"]),
            layer=str(proposal["layer"]),
            target_rule_id=proposal.get("target_rule_id"),
            proposed_rule=proposal.get("proposed_rule"),
        )
        bundle = {
            "schema_version": "1.0",
            "proposal": proposal,
            "diff": diff,
            "source_refs": list(proposal.get("source_refs") or ()),
            "rationale": proposal.get("rationale"),
            "impact": dict(proposal.get("impact") or {}),
            "regression_checklist": [{**c, "execution_status": "pending"} for c in _REGRESSION_CHECKLIST],
            "status": proposal.get("status"),
            "governance": governance,
            "evidence_records": self._evidence_records(proposal_id),
            "ready_for_public_release": False,
            "public_regression_executed": False,
            "export_notice": "用于交流或独立知识治理审查；检查通过与人工确认均不等于公共发布，待执行清单不得当作已通过。",
            # 导出包始终声明未生效，避免被误当作已发布规则
            "effective": False,
            "exported_at": _now(),
            "exported_by": proposal.get("created_by"),
            "bundle_sha256": "sha256:" + "0" * 64,
        }
        bundle["bundle_sha256"] = models.content_hash(
            bundle, exclude=("bundle_sha256", "exported_at")
        )
        return bundle

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def _audit_path(self, proposal_id: str) -> Path:
        return paths.contained_path(self.root, proposal_id, "events.jsonl")

    def _audit(self, proposal_id: str, event_kind: str, actor: str,
               detail: Optional[Mapping[str, Any]] = None) -> None:
        path = self._audit_path(proposal_id)
        private_fs.secure_dir(path.parent)
        event = {
            "event_id": f"ev_{uuid.uuid4().hex[:12]}",
            "proposal_id": proposal_id,
            "event_kind": event_kind,
            "actor": actor,
            "recorded_at": _now(),
            "result": "ok",
        }
        if detail:
            event.update({k: v for k, v in detail.items() if v is not None})
        private_fs.secure_append_line(path, json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def list_audit_events(self, proposal_id: str) -> List[Dict[str, Any]]:
        path = self._audit_path(proposal_id)
        if not path.is_file():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

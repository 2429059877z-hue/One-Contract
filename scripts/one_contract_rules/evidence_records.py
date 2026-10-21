"""Immutable local evidence bound to a rule's substantive content and version.

Human approval is an explicit local operator attestation, not authentication or
an assertion that an automated legal review ran. Regression records are written
only by the trusted application executor, never by an arbitrary HTTP upload.
"""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from . import models, paths, private_fs

class EvidenceError(ValueError):
    pass

# Every other field, including version, title, scope, conditions and source refs,
# is substantive. State transitions must not invalidate their own evidence.
_METADATA = {"content_sha256", "revision", "created_at", "created_by", "updated_at", "updated_by", "approval_status", "activation_status", "review"}

def semantic_hash(rule: Mapping[str, Any]) -> str:
    return models.content_hash({k: v for k, v in rule.items() if k not in _METADATA}, exclude=())

def rule_set_hash(rules) -> str:
    """Bind the complete intended set, independent of lifecycle bookkeeping."""
    subjects = {r["client_rule_id"]: semantic_hash(r) for r in rules}
    if len(subjects) != len(rules):
        raise EvidenceError("规则集合中存在重复编号。")
    return models.content_hash({"rules": subjects}, exclude=())

def _path(profile_dir: Path, evidence_id: str) -> Path:
    if not isinstance(evidence_id, str) or not re.fullmatch(r"ev_[a-z0-9][a-z0-9_-]{7,95}", evidence_id):
        raise EvidenceError("证据编号无效。")
    return paths.contained_path(profile_dir, "evidence", evidence_id + ".json")

def validate_report(report: Mapping[str, Any]) -> bool:
    if not isinstance(report, Mapping) or report.get("scope") not in ("rule_resolution", "rule_set_resolution"):
        raise EvidenceError("测试报告必须明确为规则解析测试。")
    if report["scope"] == "rule_set_resolution" and not re.fullmatch(r"sha256:[a-f0-9]{64}", str(report.get("target_set_sha256", ""))):
        raise EvidenceError("整组测试报告必须绑定完整目标规则集合。")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(report.get("public_asset_fingerprint", ""))) or "base_client_snapshot_id" not in report:
        raise EvidenceError("测试报告必须绑定公共指纹及客户基线快照。")
    executor = report.get("executor")
    if not isinstance(executor, Mapping) or not all(isinstance(executor.get(k), str) and executor[k].strip() for k in ("name", "version")) or not report.get("suite_version"):
        raise EvidenceError("测试报告缺少执行器或样例集版本。")
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvidenceError("至少执行一个具有预期结果的真实样例。")
    seen = set()
    for case in cases:
        if not isinstance(case, Mapping) or not all(k in case for k in ("case_id", "input", "expected", "actual", "passed")):
            raise EvidenceError("测试样例缺少输入、预期或实际结果。")
        if not isinstance(case["case_id"], str) or not case["case_id"] or case["case_id"] in seen:
            raise EvidenceError("测试样例编号必须非空且唯一。")
        seen.add(case["case_id"])
        if type(case["passed"]) is not bool or case["passed"] != (case["expected"] == case["actual"]):
            raise EvidenceError("测试结论与预期/实际结果不一致。")
    return all(case["passed"] for case in cases)

def write(profile_dir: Path, rule: Mapping[str, Any], *, kind: str, actor: str,
          evidence_id: str | None = None, report: Mapping[str, Any] | None = None) -> dict:
    if kind not in ("human_approval", "executed_regression"):
        raise EvidenceError("未知证据种类。")
    passed = validate_report(report) if kind == "executed_regression" else True
    evidence_id = evidence_id or "ev_" + uuid.uuid4().hex
    target = _path(profile_dir, evidence_id)
    if target.exists():
        raise EvidenceError("证据编号已存在，禁止覆盖。")
    record = {
        "schema_version": "1.0", "evidence_id": evidence_id, "evidence_kind": kind,
        "client_profile_id": rule["client_profile_id"], "client_rule_id": rule["client_rule_id"],
        "rule_version": rule["version"], "subject_sha256": semantic_hash(rule),
        "executed_revision": rule["revision"] if kind == "executed_regression" else None,
        "actor": actor, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "result": "passed" if passed else "failed",
        "report": dict(report) if report is not None else None,
        "input_sha256": models.content_hash({"values": [c["input"] for c in report["cases"]]}, exclude=()) if report else None,
        "output_sha256": models.content_hash({"values": [c["actual"] for c in report["cases"]]}, exclude=()) if report else None,
    }
    record["content_sha256"] = models.content_hash(record)
    private_fs.atomic_write_json(target, record)
    return record

def load(profile_dir: Path, evidence_id: str, rule: Mapping[str, Any], *, kind: str) -> dict:
    target = _path(profile_dir, evidence_id)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError("证据不存在或无法读取；不能以声明代替实际记录。") from exc
    if not isinstance(record, dict) or record.get("content_sha256") != models.content_hash(record):
        raise EvidenceError("证据内容哈希不匹配。")
    expected = {"evidence_id": evidence_id, "evidence_kind": kind, "client_profile_id": rule["client_profile_id"],
                "client_rule_id": rule["client_rule_id"], "rule_version": rule["version"], "subject_sha256": semantic_hash(rule)}
    if any(record.get(k) != v for k, v in expected.items()):
        raise EvidenceError("证据与当前客户、规则、内容或版本不匹配。")
    if kind == "executed_regression":
        report = record.get("report")
        passed = validate_report(report)
        if record.get("input_sha256") != models.content_hash({"values": [c["input"] for c in report["cases"]]}, exclude=()) or record.get("output_sha256") != models.content_hash({"values": [c["actual"] for c in report["cases"]]}, exclude=()):
            raise EvidenceError("测试输入输出哈希不匹配。")
        if record.get("result") != ("passed" if passed else "failed"):
            raise EvidenceError("证据结果与实际样例不一致。")
    return record

def verify_execution_context(profile_dir, rule, report, *, public_fingerprint, active_snapshot_id,
                             target_set_sha256=None):
    if report.get("public_asset_fingerprint") != public_fingerprint:
        raise EvidenceError("测试后的公共规则版本已变化，请重新执行测试。")
    if report.get("scope") == "rule_set_resolution":
        if not target_set_sha256 or report.get("target_set_sha256") != target_set_sha256:
            raise EvidenceError("拟发布集合与整组测试内容不同，请重新执行整组测试（BATCH_TEST_REQUIRED）。")
        # This executor replaces the entire client overlay; unlike a single-rule
        # test it does not borrow rules from the previous active snapshot.
        return
    from .publisher import Publisher, PublishError
    pub = Publisher(profile_dir)
    def ambient(snapshot_id):
        if not snapshot_id:
            return {}
        try:
            rules = pub.load_snapshot(snapshot_id)["rules_doc"]["rules"]
        except PublishError as exc:
            raise EvidenceError("测试基线快照缺失或不属于当前客户。") from exc
        return {r["client_rule_id"]: semantic_hash(r) for r in rules if r["client_rule_id"] != rule["client_rule_id"]}
    if ambient(report.get("base_client_snapshot_id")) != ambient(active_snapshot_id):
        raise EvidenceError("测试后的其他客户规则已变化，请重新执行测试。")


def publication_refs(profile_dir: Path, rule: Mapping[str, Any], *, public_fingerprint=None,
                     active_snapshot_id=None, target_set_sha256=None, require_rule_set=False) -> list[dict]:
    review = rule.get("review") or {}
    approval = review.get("lawyer_approval_evidence_id")
    regressions = review.get("regression_evidence_ids") or []
    if not approval or not regressions:
        raise EvidenceError("发布须有绑定当前内容的人工确认和实际测试记录。")
    records = [load(profile_dir, approval, rule, kind="human_approval")]
    records += [load(profile_dir, eid, rule, kind="executed_regression") for eid in regressions]
    if require_rule_set and any(r["report"].get("scope") != "rule_set_resolution"
            or r["report"].get("target_set_sha256") != target_set_sha256
            or r["report"].get("executor", {}).get("name") != "one-contract-production-resolver-batch"
            for r in records[1:]):
        raise EvidenceError("多条规则组成的新集合须由完整集合场景验证；单条测试不能拼作整组证据（BATCH_TEST_REQUIRED）。")
    if public_fingerprint is not None:
        for record in records[1:]:
            verify_execution_context(profile_dir, rule, record["report"],
                public_fingerprint=public_fingerprint, active_snapshot_id=active_snapshot_id,
                target_set_sha256=target_set_sha256)
    if any(r.get("result") != "passed" for r in records):
        raise EvidenceError("发布证据含未通过记录。")
    return [{"evidence_id": r["evidence_id"], "evidence_kind": r["evidence_kind"],
             "relative_path": "evidence/" + r["evidence_id"] + ".json",
             "sha256": r["content_sha256"], "subject_sha256": r["subject_sha256"]} for r in records]

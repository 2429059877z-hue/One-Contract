#!/usr/bin/env python3
"""客户档案、规则草稿、生命周期与发布的存储层。

契约来源：architecture-contract.md §8（客户数据模型）、§9（数据根与路径安全）、
§10（并发与原子性）、§11（错误码）；RS-HC-003/004/005；ADR-005/007/018/023。

数据布局（全部在 `data_root` 之外无写入）：
    <data-root>/clients/<profile-id>/
        profile.json
        drafts/<rule-id>.json
        snapshots/<snapshot-id>/{manifest.json,rules.json}
        active-manifest.json
        audit/events.jsonl

硬边界：
- 客户数据**绝不**写入 Skill 目录（RS-HC-003）；
- 保存草稿不隐式发布（RS-HC-004）；
- 已发布规则不做物理删除，只能以新快照停用或 deprecated（ADR-018 / CR-006）；
- 路径一律经 `contained_path` 校验，拒绝点段、绝对路径与符号链接越界。
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from functools import wraps
import inspect
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import audit, models, paths, publisher, validator, private_fs, platform_fs, evidence_records, guard_catalog
from .transaction_lock import profile_lock

__all__ = [
    "ACTIVE_PROFILE_STATUSES",
    "ClientRepository",
    "ClientStoreError",
    "RevisionConflict",
    "SnapshotIntegrityError",
]

#: 允许继续操作的 profile 状态
ACTIVE_PROFILE_STATUSES = ("active",)

#: 允许发布的规则状态（必须已完成律师批准与回归）
PUBLISHABLE_APPROVAL_STATES = ("regression_passed", "active")

_DIR_MODE = 0o700
_FILE_MODE = 0o600


class ClientStoreError(Exception):
    """客户存储操作失败。调用方不得降级为部分写入。"""


class RevisionConflict(ClientStoreError):
    """expected_revision 不匹配（DRAFT_REVISION_CONFLICT）。"""


class ProfileNotFound(ClientStoreError):
    """客户档案不存在。HTTP 层据此映射 404 CLIENT_PROFILE_NOT_FOUND。"""


class InvalidIdentifier(ClientStoreError):
    """标识符形态非法（点段、绝对路径、大小写或长度不符契约）。

    与「不存在」区分开：前者是调用方输入非法（400），后者是资源缺失（404）。
    """


class SnapshotIntegrityError(ClientStoreError):
    """快照完整性校验失败（SNAPSHOT_INTEGRITY_FAILED）。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: 证据 ID 形态由 `governance-evidence-v1` 冻结（其 evidenceReference.evidence_id）。
#: 在**记录时**即校验：非法 ID 会让规则永远无法发布，早报比等到发布才报好
#: （此前只由快照 schema 在发布时兜住，而那个校验此前根本没接线）。
_EVIDENCE_ID_RE = re.compile(r"^ev_[a-z0-9][a-z0-9_-]{7,95}$")
#: 公开别名：API 层需要在调用前做**字段级**校验（CR-002 要求校验失败时定位到具体字段），
#: 但不得复制这份正则——冻结格式只有这一处来源。
EVIDENCE_ID_PATTERN = _EVIDENCE_ID_RE


def _require_valid_evidence_id(evidence_id: str) -> str:
    if not isinstance(evidence_id, str) or not _EVIDENCE_ID_RE.match(evidence_id):
        raise ClientStoreError(
            f"证据 ID 不符合冻结契约（应为 ev_ 开头、总长 11–99 的小写标识）："
            f"{evidence_id!r}"
        )
    return evidence_id


def _secure_dir(path: Path) -> None:
    private_fs.secure_dir(path)


def _secure_write(path: Path, payload: Mapping[str, Any]) -> None:
    private_fs.atomic_write_json(path, payload)


def _transaction(method):
    signature = inspect.signature(method)
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self.read_only:
            raise ClientStoreError("仓库为只读模式，拒绝写入。")
        bound = signature.bind(self, *args, **kwargs)
        profile_id = bound.arguments.get("profile_id") or bound.arguments.get("client_profile_id")
        with profile_lock(self._resolve_profile_dir(profile_id)):
            self._recover_validation_commit(self._resolve_profile_dir(profile_id))
            return method(self, *args, **kwargs)
    return wrapped


def _reset_review(rule):
    rule["approval_status"] = "candidate"
    rule["activation_status"] = "inactive"
    review = dict(rule.get("review") or {})
    review.update({"lawyer_approval_evidence_id": None, "lawyer_approved_by": None,
        "lawyer_approved_at": None, "legal_check_status": "not_checked",
        "legal_checked_at": None, "regression_evidence_ids": []})
    rule["review"] = review


def _check_revision(rule, expected_revision):
    if expected_revision is not None and int(rule.get("revision", 0)) != expected_revision:
        raise RevisionConflict("规则已修改，请刷新后重新确认（DRAFT_REVISION_CONFLICT）。")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ClientStoreError(f"文件不存在: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClientStoreError(f"文件损坏: {path.name}") from exc


def _with_hash(rule: Mapping[str, Any]) -> Dict[str, Any]:
    body = {k: v for k, v in rule.items() if k != "content_sha256"}
    body["content_sha256"] = models.content_hash(body)
    return body


class ClientRepository:
    """外置 data root 上的客户数据仓库。"""

    def __init__(self, *, data_root: Path, read_only: bool = False) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.clients_root = self.data_root / "clients"
        self.read_only = read_only
        self.recovery_warnings = []
        if not read_only:
            _secure_dir(self.data_root)
            _secure_dir(self.clients_root)
            for directory in self.clients_root.iterdir():
                if paths.is_safe_profile_id(directory.name) and (directory / "profile.json").is_file():
                    try:
                        with profile_lock(self._resolve_profile_dir(directory.name)):
                            self._recover_validation_commit(self._resolve_profile_dir(directory.name))
                            publisher.Publisher(self._resolve_profile_dir(directory.name)).recover_pending_commits()
                    except (ClientStoreError, publisher.PublishError, audit.AuditError, OSError) as exc:
                        # Other clients remain usable; operations on this client
                        # still fail closed until its pending commit can recover.
                        self.recovery_warnings.append({"client_profile_id": directory.name, "error": str(exc)})

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def _resolve_profile_dir(self, profile_id: str) -> Path:
        if not paths.is_safe_profile_id(profile_id):
            raise ClientStoreError(
                f"不安全的 profile ID: {profile_id!r}；只允许小写字母数字与 _-，且必须以字母数字开头。"
            )
        try:
            target = paths.contained_path(self.clients_root, profile_id)
        except paths.PathContractError as exc:
            raise ClientStoreError(str(exc)) from exc
        # 二次校验：resolve 后必须仍在 clients 根内（覆盖符号链接越界）
        if target.exists():
            real = target.resolve()
            if real != self.clients_root and self.clients_root not in real.parents:
                raise ClientStoreError("profile 目录越出 data root，拒绝访问。")
        return target

    def _require_profile_dir(self, profile_id: str) -> Path:
        profile_dir = self._resolve_profile_dir(profile_id)
        if not (profile_dir / "profile.json").is_file():
            # SEC-005：不存在即 fail closed，不退回最近客户
            raise ProfileNotFound(f"客户档案不存在: {profile_id}")
        try:
            profile_path = paths.contained_path(profile_dir, "profile.json")
            profile = _read_json(profile_path)
            validator.validate_client_profile(profile)
            if profile.get("client_profile_id") != profile_id or profile.get("content_sha256") != models.content_hash(profile):
                raise ClientStoreError("客户档案归属或内容哈希不匹配。")
        except paths.PathContractError as exc:
            raise ClientStoreError(str(exc)) from exc
        return profile_dir

    def _require_active_profile_dir(self, profile_id: str) -> Path:
        profile_dir = self._require_profile_dir(profile_id)
        profile = _read_json(profile_dir / "profile.json")
        if profile.get("status") not in ACTIVE_PROFILE_STATUSES:
            raise ClientStoreError(
                f"客户档案状态为 {profile.get('status')!r}，不可写入（DATA-005）。"
            )
        return profile_dir

    # ------------------------------------------------------------------ #
    # Profile
    # ------------------------------------------------------------------ #
    @_transaction
    def create_client_profile(
        self, *, client_profile_id: str, display_name: str, actor: str
    ) -> Dict[str, Any]:
        profile_dir = self._resolve_profile_dir(client_profile_id)
        if (profile_dir / "profile.json").is_file():
            raise ClientStoreError(f"客户档案已存在: {client_profile_id}")

        now = _now()
        profile = {
            "schema_version": "1.0",
            "client_profile_id": client_profile_id,
            "display_name": display_name,
            "selection_policy": "explicit",
            "status": "active",
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "created_by": actor,
            "updated_by": actor,
            "content_sha256": "sha256:" + "0" * 64,
        }
        try:
            validator.validate_client_profile(_with_hash(profile))
        except validator.ValidationError as exc:
            raise InvalidIdentifier(f"档案不符合契约: {exc}") from exc

        if profile_dir.exists():
            raise ClientStoreError("客户目录已存在但未完成建档，请先恢复或检查该目录。")
        staging = self.clients_root / (".create-" + client_profile_id + "-" + uuid.uuid4().hex)
        try:
            _secure_dir(staging)
            _secure_write(staging / "profile.json", _with_hash(profile))
            publisher._atomic_write_json(staging / publisher.ACTIVE_MANIFEST_NAME,
                                         publisher.empty_active_manifest(client_profile_id))
            audit.append_event(staging, event_kind="profile_created", actor=actor,
                               detail={"client_profile_id": client_profile_id})
            platform_fs.replace(staging, profile_dir)
        except Exception:
            # If the rename committed before a directory-flush error, return the
            # complete profile; no partial directory is exposed as a live client.
            if not (profile_dir / "profile.json").is_file():
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return _read_json(profile_dir / "profile.json")

    def get_client_profile(self, profile_id: str) -> Dict[str, Any]:
        profile_dir = self._require_profile_dir(profile_id)
        return _read_json(profile_dir / "profile.json")

    @_transaction
    def deactivate_client_profile(self, profile_id: str, *, actor: str) -> Dict[str, Any]:
        profile_dir = self._require_profile_dir(profile_id)
        profile = _read_json(profile_dir / "profile.json")
        profile["status"] = "inactive"
        profile["revision"] = int(profile.get("revision", 1)) + 1
        profile["updated_at"] = _now()
        profile["updated_by"] = actor
        _secure_write(profile_dir / "profile.json", _with_hash(profile))
        audit.append_event(profile_dir, event_kind="profile_deactivated", actor=actor,
                           detail={"client_profile_id": profile_id})
        return _read_json(profile_dir / "profile.json")

    # ------------------------------------------------------------------ #
    # 草稿
    # ------------------------------------------------------------------ #
    def _draft_path(self, profile_dir: Path, rule_id: str) -> Path:
        if not isinstance(rule_id, str) or not re.fullmatch(r"cr_[a-z0-9][a-z0-9_-]{7,95}", rule_id):
            raise InvalidIdentifier("规则编号无效，不能使用文件路径。")
        try:
            return paths.contained_path(profile_dir, "drafts", f"{rule_id}.json")
        except paths.PathContractError as exc:
            raise ClientStoreError(str(exc)) from exc

    def _read_validation_commit(self, profile_dir: Path) -> Optional[Dict[str, Any]]:
        """The durable journal is the logical commit fact for a complete set.

        Drafts may be physically copied in several steps; while the journal
        exists, reads expose its complete committed set, including after a crash.
        """
        if not profile_dir.is_dir():
            return None
        target = paths.contained_path(profile_dir, "validation-commit.json")
        if not target.is_file():
            return None
        journal = _read_json(target)
        drafts = journal.get("drafts")
        before = journal.get("before_sha256")
        if (journal.get("content_sha256") != models.content_hash(journal)
                or journal.get("client_profile_id") != profile_dir.name
                or not re.fullmatch(r"vtx_[a-f0-9]{32}", str(journal.get("transaction_id", "")))
                or not isinstance(drafts, dict) or not drafts
                or not isinstance(before, dict) or set(before) != set(drafts)
                or type(journal.get("passed")) is not bool):
            raise ClientStoreError("整组测试提交记录损坏；保留恢复现场，拒绝覆盖草稿。")
        for rule_id, draft in drafts.items():
            path = self._draft_path(profile_dir, rule_id)
            if (not isinstance(draft, dict) or draft.get("client_profile_id") != profile_dir.name
                    or draft.get("client_rule_id") != rule_id
                    or draft.get("content_sha256") != models.content_hash(draft)):
                raise ClientStoreError("整组测试恢复内容不匹配。")
            current = _read_json(path)
            if (current.get("content_sha256") != models.content_hash(current)
                    or current["content_sha256"] not in (before[rule_id], draft["content_sha256"])):
                raise ClientStoreError("整组测试提交后草稿被外部修改；拒绝自动覆盖，请先检查恢复记录。")
        try:
            target_hash = evidence_records.rule_set_hash(list(drafts.values()))
            if journal.get("target_set_sha256") != target_hash:
                raise evidence_records.EvidenceError("整组测试恢复集合不匹配。")
            for draft in drafts.values():
                evidence_records.load(profile_dir, draft["review"]["lawyer_approval_evidence_id"], draft, kind="human_approval")
                regressions = draft["review"]["regression_evidence_ids"]
                if len(regressions) != 1:
                    raise evidence_records.EvidenceError("整组测试恢复证据不完整。")
                record = evidence_records.load(profile_dir, regressions[0], draft, kind="executed_regression")
                if (record["report"].get("target_set_sha256") != target_hash
                        or record["report"].get("scope") != "rule_set_resolution"
                        or record["result"] != ("passed" if journal["passed"] else "failed")
                        or draft["approval_status"] != ("regression_passed" if journal["passed"] else "lawyer_approved")):
                    raise evidence_records.EvidenceError("整组测试恢复结果与状态不匹配。")
        except evidence_records.EvidenceError as exc:
            raise ClientStoreError(str(exc)) from exc
        return journal

    def _recover_validation_commit(self, profile_dir: Path) -> None:
        """Replay a committed complete-set result, idempotently under the lock."""
        journal = self._read_validation_commit(profile_dir)
        if not journal:
            return
        for rule_id, draft in journal["drafts"].items():
            path = self._draft_path(profile_dir, rule_id)
            if _read_json(path)["content_sha256"] != draft["content_sha256"]:
                _secure_write(path, draft)
            match = {"transaction_id": journal["transaction_id"], "client_rule_id": rule_id}
            if not audit.has_event(profile_dir, "regression_recorded", **match):
                audit.append_event(profile_dir, event_kind="regression_recorded", actor=journal["actor"],
                    result="ok" if journal["passed"] else "failed",
                    detail={**match, "target_set_sha256": journal["target_set_sha256"],
                            "evidence_id": draft["review"]["regression_evidence_ids"][0],
                            "rule_version": draft["version"]})
        paths.contained_path(profile_dir, "validation-commit.json").unlink()
        platform_fs.sync_directory(profile_dir)

    def _load_draft_or_none(self, profile_dir: Path, rule_id: str) -> Optional[Dict[str, Any]]:
        path = self._draft_path(profile_dir, rule_id)
        pending = self._read_validation_commit(profile_dir)
        if pending and rule_id in pending["drafts"]:
            return pending["drafts"][rule_id]
        if not path.is_file():
            return None
        rule = _read_json(path)
        if rule.get("client_profile_id") != profile_dir.name or rule.get("client_rule_id") != rule_id or rule.get("content_sha256") != models.content_hash(rule):
            raise ClientStoreError("草稿归属、编号或内容哈希不匹配。")
        return rule

    def get_draft(self, profile_id: str, rule_id: str) -> Optional[Dict[str, Any]]:
        profile_dir = self._require_profile_dir(profile_id)
        with nullcontext() if self.read_only else profile_lock(profile_dir):
            return self._load_draft_or_none(profile_dir, rule_id)

    def list_drafts(self, profile_id: str) -> List[Dict[str, Any]]:
        profile_dir = self._require_profile_dir(profile_id)
        with nullcontext() if self.read_only else profile_lock(profile_dir):
            drafts_dir = profile_dir / "drafts"
            if not drafts_dir.is_dir():
                return []
            return [
                self._load_draft_or_none(profile_dir, p.stem) for p in sorted(drafts_dir.glob("*.json"))
                if not p.name.startswith(".tmp-")
            ]

    def _validate_rule_for_save(self, profile_id: str, rule: Mapping[str, Any]) -> Dict[str, Any]:
        if rule.get("client_profile_id") != profile_id:
            raise ClientStoreError(
                f"规则归属 {rule.get('client_profile_id')!r} 与目标客户 {profile_id!r} 不一致（CR-008）。"
            )
        from .material_sources import MaterialSources
        try:
            MaterialSources(self.data_root, read_only=True).validate_refs(rule.get("source_refs"), client_profile_id=profile_id)
        except (ValueError, OSError) as exc:
            raise ClientStoreError("私有材料来源校验失败：" + str(exc)) from exc
        try:
            validator.validate_client_rule(rule)
            validator.validate_rule_business(rule)
        except validator.ValidationError:
            raise
        return dict(rule)

    @_transaction
    def create_new_rule(
        self, profile_id: str, rule: Mapping[str, Any], *, actor: str
    ) -> Dict[str, Any]:
        """新建规则。已存在同 ID 时拒绝，不覆盖（CR-007）。"""
        profile_dir = self._require_active_profile_dir(profile_id)
        rule_id = str(rule.get("client_rule_id") or "")
        if not rule_id:
            raise ClientStoreError("规则缺少 client_rule_id。")
        if self._load_draft_or_none(profile_dir, rule_id) is not None:
            raise ClientStoreError(f"规则 ID 已存在，拒绝覆盖: {rule_id}")

        # 内容哈希由仓库计算，调用方无需提供：schema 把它列为必需字段，但值只可能由
        # 本层算出——强迫调用方先编一个占位哈希再被覆写，是没有意义的契约摩擦。
        # （`_with_hash` 会把传入值覆盖为真实哈希，故此处先补位再校验。）
        incoming = dict(rule)
        _reset_review(incoming)
        prepared = self._validate_rule_for_save(profile_id, _with_hash(incoming))
        prepared["revision"] = 1
        stored = _with_hash(prepared)
        _secure_write(self._draft_path(profile_dir, rule_id), stored)
        audit.append_event(profile_dir, event_kind="draft_created", actor=actor,
                           detail={"client_rule_id": rule_id, "rule_version": stored.get("version"),
                                     "content_sha256": stored["content_sha256"]})
        return stored

    @_transaction
    def save_draft(
        self,
        profile_id: str,
        rule: Mapping[str, Any],
        *,
        expected_revision: int,
        actor: str,
    ) -> Dict[str, Any]:
        """保存草稿。草稿不存在时按新建处理。保存**不**触发发布（RS-HC-004）。"""
        profile_dir = self._require_active_profile_dir(profile_id)
        rule_id = str(rule.get("client_rule_id") or "")
        if not rule_id:
            raise ClientStoreError("规则缺少 client_rule_id。")

        existing = self._load_draft_or_none(profile_dir, rule_id)

        # CR-009：先做并发比较交换——调用方视图过期时不得继续。
        current_revision = int(existing.get("revision", 0)) if existing else 0
        if current_revision != expected_revision:
            raise RevisionConflict(
                f"草稿已被其他操作修改：期望 revision={expected_revision}，"
                f"当前 revision={current_revision}（DRAFT_REVISION_CONFLICT）。"
            )

        # CR-004：已发布规则**不得就地改写**。
        #
        # 原实现把这段挂在 `existing.approval_status == "active"` 之下，而**发布并不会把草稿
        # 置为 active**（active 是快照里那份规则的属性，草稿仍是 regression_passed）——
        # 于是该分支在真实流程里从不触发：实测同一版本号 1.0.0 可被就地改写、标题已变而版本未变。
        # 判定条件改为「该版本号是否已出现在生效快照里」，与草稿状态无关。
        published_versions = self._active_version_set(profile_dir, rule_id)
        incoming_version = str(rule.get("version") or "")
        if published_versions and incoming_version in published_versions:
            raise ClientStoreError(
                f"版本 {incoming_version!r} 已发布，修改已发布规则必须提升版本号（CR-004）："
                "已发布的版本不得就地改写（旧 active 与其哈希必须保持不变）。"
            )

        incoming = dict(rule)
        changed = existing is None or evidence_records.semantic_hash(incoming) != evidence_records.semantic_hash(existing)
        if changed:
            _reset_review(incoming)
        elif existing:
            for key in ("approval_status", "activation_status", "review"):
                incoming[key] = existing.get(key)
        prepared = self._validate_rule_for_save(profile_id, _with_hash(incoming))
        is_new_version = bool(published_versions) and existing is not None

        prepared["revision"] = current_revision + 1 if existing else 1
        prepared["updated_at"] = _now()
        prepared["updated_by"] = actor
        stored = _with_hash(prepared)
        _secure_write(self._draft_path(profile_dir, rule_id), stored)
        audit.append_event(profile_dir, event_kind="draft_saved", actor=actor,
                           detail={"client_rule_id": rule_id,
                                     "new_version": is_new_version,
                                     "rule_version": stored.get("version"),
                                     "content_sha256": stored["content_sha256"]})
        return stored

    @_transaction
    def delete_draft(
        self, profile_id: str, rule_id: str, *, confirmation: bool, actor: str
    ) -> None:
        profile_dir = self._require_profile_dir(profile_id)
        draft = self._load_draft_or_none(profile_dir, rule_id)
        if draft is None:
            raise ClientStoreError(f"草稿不存在: {rule_id}")
        if not confirmation:
            raise ClientStoreError("删除草稿需要二次确认（CR-005）。")
        if draft.get("approval_status") == "active" or self._is_published(profile_dir, rule_id):
            # CR-006 / ADR-018：已发布规则禁止物理删除
            raise ClientStoreError(
                "已发布规则不得物理删除；请以新快照停用或标记 deprecated（CR-006 / ADR-018）。"
            )
        path = self._draft_path(profile_dir, rule_id)
        path.unlink()
        audit.append_event(profile_dir, event_kind="draft_deleted", actor=actor,
                           detail={"client_rule_id": rule_id, "rule_version": draft.get("version")})

    def _active_version_set(self, profile_dir: Path, rule_id: str) -> set:
        versions = set()
        pub = publisher.Publisher(profile_dir)
        manifest = pub.read_manifest()
        snapshots = pub.snapshots_dir
        for directory in sorted(snapshots.iterdir()) if snapshots.is_dir() else ():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            snapshot_id = directory.name
            doc = pub.load_snapshot(snapshot_id)
            for rule in doc["rules_doc"].get("rules", []):
                if rule.get("client_rule_id") == rule_id:
                    versions.add(rule.get("version"))
        return versions

    def _is_published(self, profile_dir: Path, rule_id: str) -> bool:
        return bool(self._active_version_set(profile_dir, rule_id))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _transition(
        self,
        profile_dir: Path,
        rule_id: str,
        *,
        allowed_from: Sequence[str],
        to_state: str,
        actor: str,
        event_kind: str,
        mutate: Optional[Any] = None,
        expected_revision: Optional[int] = None,
        event_result: str = "ok",
    ) -> Dict[str, Any]:
        draft = self._load_draft_or_none(profile_dir, rule_id)
        if draft is None:
            raise ClientStoreError(f"规则不存在: {rule_id}")
        _check_revision(draft, expected_revision)
        current = draft.get("approval_status")
        if current not in allowed_from:
            raise ClientStoreError(
                f"非法状态迁移：{current!r} → {to_state!r}（当前状态不允许该跳转，"
                f"允许来源: {list(allowed_from)}）。"
            )
        updated = dict(draft)
        updated["approval_status"] = to_state
        updated["revision"] = int(draft.get("revision", 1)) + 1
        updated["updated_at"] = _now()
        updated["updated_by"] = actor
        if mutate is not None:
            mutate(updated)
        stored = _with_hash(updated)
        _secure_write(self._draft_path(profile_dir, rule_id), stored)
        audit.append_event(profile_dir, event_kind=event_kind, actor=actor, result=event_result,
                           detail={"client_rule_id": rule_id, "to_state": to_state,
                                     "rule_version": stored.get("version")})
        return stored

    @_transaction
    def deprecate_rule(self, profile_id: str, rule_id: str, *, actor: str, expected_revision: Optional[int] = None) -> Dict[str, Any]:
        """停用一条已发布规则（CR-006 / ADR-018）。

        CR-006 写的是「禁止物理删除，**只能新快照停用或 deprecated**」——此前只实现了前半句：
        删除会被拒绝，但**没有任何路径去停用**，用户想退掉一条已发布规则会走进死路。

        本方法只把草稿标为 `deprecated`，**不改任何物理记录**；由于 `publish_snapshot`
        只收录 `PUBLISHABLE_APPROVAL_STATES`（regression_passed / active），
        下一次发布的新快照自然不再包含它——这就是「以新快照停用」。
        在重新发布之前，旧快照仍是当前生效版本，规则也仍然生效。
        """
        profile_dir = self._require_active_profile_dir(profile_id)
        return self._transition(
            profile_dir, rule_id,
            allowed_from=("regression_passed", "active"),
            to_state="deprecated",
            actor=actor, event_kind="deprecation_drafted", expected_revision=expected_revision,
        )

    @_transaction
    def submit_rule(self, profile_id: str, rule_id: str, *, actor: str, expected_revision: Optional[int] = None) -> Dict[str, Any]:
        """提交规则进入审批流。提交本身不改变审批状态，只记录事件并递增 revision。"""
        profile_dir = self._require_active_profile_dir(profile_id)
        return self._transition(
            profile_dir, rule_id,
            allowed_from=("candidate", "draft"), to_state="candidate",
            actor=actor, event_kind="submitted", expected_revision=expected_revision,
        )

    @_transaction
    def record_lawyer_approval(
        self, profile_id: str, rule_id: str, *, evidence_id: str, actor: str, expected_revision: Optional[int] = None
    ) -> Dict[str, Any]:
        _require_valid_evidence_id(evidence_id)
        profile_dir = self._require_active_profile_dir(profile_id)

        def _mutate(rule: Dict[str, Any]) -> None:
            try:
                evidence_records.write(profile_dir, rule, kind="human_approval", actor=actor, evidence_id=evidence_id)
            except evidence_records.EvidenceError as exc:
                raise ClientStoreError(str(exc)) from exc
            review = dict(rule.get("review") or {})
            review.update({
                "lawyer_approval_evidence_id": evidence_id,
                "lawyer_approved_by": actor,
                "lawyer_approved_at": _now(),
                "legal_check_status": "checked",
                "legal_checked_at": _now(),
            })
            rule["review"] = review

        return self._transition(
            profile_dir, rule_id,
            allowed_from=("candidate",), to_state="lawyer_approved",
            actor=actor, event_kind="lawyer_approved", mutate=_mutate, expected_revision=expected_revision,
        )

    @_transaction
    def record_regression_result(
        self, profile_id: str, rule_id: str, *,
        evidence_id: str, passed: bool, actor: str, expected_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        _require_valid_evidence_id(evidence_id)
        profile_dir = self._require_active_profile_dir(profile_id)
        draft = self._load_draft_or_none(profile_dir, rule_id)
        if draft is None:
            raise ClientStoreError(f"规则不存在: {rule_id}")
        _check_revision(draft, expected_revision)
        try:
            record = evidence_records.load(profile_dir, evidence_id, draft, kind="executed_regression")
        except evidence_records.EvidenceError as exc:
            raise ClientStoreError(str(exc)) from exc
        if type(passed) is not bool or passed != (record["result"] == "passed"):
            raise ClientStoreError("调用方声明与实际执行证据不一致。")
        if record.get("executed_revision") != draft["revision"]:
            raise RevisionConflict("测试记录对应的草稿版本已改变；不能重用旧通过覆盖最新结果（DRAFT_REVISION_CONFLICT）。")
        review = draft.get("review") or {}
        if not review.get("lawyer_approval_evidence_id"):
            # LIFE-003：缺审批不得进入后续状态
            raise ClientStoreError("缺少律师批准证据，不得记录回归结果（LIFE-003）。")
        if draft.get("approval_status") not in ("lawyer_approved", "regression_passed", "active"):
            raise ClientStoreError(
                f"当前状态 {draft.get('approval_status')!r} 不允许记录回归结果。"
            )
        if not passed:
            audit.append_event(profile_dir, event_kind="regression_recorded", actor=actor,
                               result="failed",
                               detail={"client_rule_id": rule_id, "evidence_id": evidence_id,
                                         "rule_version": draft.get("version")})
            raise ClientStoreError("回归未通过，不允许发布（LIFE-004）。")

        def _mutate(rule: Dict[str, Any]) -> None:
            rev = dict(rule.get("review") or {})
            rev["regression_evidence_ids"] = [evidence_id]
            rule["review"] = rev

        return self._transition(
            profile_dir, rule_id,
            allowed_from=("lawyer_approved", "regression_passed", "active"), to_state="regression_passed",
            actor=actor, event_kind="regression_recorded", mutate=_mutate, expected_revision=expected_revision,
        )

    @_transaction
    def store_regression_evidence(self, profile_id: str, rule_id: str, *, report: Mapping[str, Any],
                                  expected_revision: int, actor: str) -> Dict[str, Any]:
        """Trusted executor API; never expose arbitrary report uploads over HTTP."""
        profile_dir = self._require_active_profile_dir(profile_id)
        draft = self._load_draft_or_none(profile_dir, rule_id)
        if draft is None:
            raise ClientStoreError("规则不存在。")
        _check_revision(draft, expected_revision)
        try:
            passed = evidence_records.validate_report(report)
            evidence_records.verify_execution_context(profile_dir, draft, report,
                public_fingerprint=report["public_asset_fingerprint"],
                active_snapshot_id=publisher.Publisher(profile_dir).read_manifest().get("active_snapshot_id"))
            record = evidence_records.write(profile_dir, draft, kind="executed_regression", actor=actor, report=report)
        except evidence_records.EvidenceError as exc:
            raise ClientStoreError(str(exc)) from exc
        if not passed and draft["approval_status"] in ("lawyer_approved", "regression_passed", "active"):
            def invalidate(rule):
                rule["review"] = {**rule["review"], "regression_evidence_ids": [record["evidence_id"]]}
                rule["activation_status"] = "inactive"
            self._transition(profile_dir, rule_id, allowed_from=("lawyer_approved", "regression_passed", "active"),
                to_state="lawyer_approved", actor=actor, event_kind="regression_recorded", event_result="failed",
                mutate=invalidate, expected_revision=expected_revision)
        return record

    @_transaction
    def store_rule_set_evidence(self, profile_id: str, *, report: Mapping[str, Any],
                               expected_revisions: Mapping[str, int],
                               expected_active_snapshot: Optional[str], actor: str) -> Dict[str, Any]:
        """Commit trusted executor evidence and the complete set's lifecycle.

        No HTTP endpoint may upload a report to this method. The scenario runner
        supplies actual resolver output and the revisions frozen before running.
        Evidence files precede the journal; interrupted precommit work is inert.
        """
        profile_dir = self._require_active_profile_dir(profile_id)
        current_id = publisher.Publisher(profile_dir).read_manifest().get("active_snapshot_id")
        if current_id != expected_active_snapshot or report.get("base_client_snapshot_id") != current_id:
            raise ClientStoreError("测试期间现行快照已变化，请重新测试（ACTIVE_SNAPSHOT_CONFLICT）。")
        drafts = [d for d in self.list_drafts(profile_id)
                  if d["approval_status"] in ("lawyer_approved", "regression_passed", "active")]
        if (not drafts or not isinstance(expected_revisions, Mapping)
                or any(type(v) is not int or v < 1 for v in expected_revisions.values())
                or {d["client_rule_id"]: d["revision"] for d in drafts} != dict(expected_revisions)):
            raise RevisionConflict("拟测试集合或规则内容已变化，请重新载入后测试（DRAFT_REVISION_CONFLICT）。")
        records = {}
        try:
            passed = evidence_records.validate_report(report)
            target_hash = evidence_records.rule_set_hash(drafts)
            if (report.get("scope") != "rule_set_resolution"
                    or report.get("target_set_sha256") != target_hash
                    or report.get("executor", {}).get("name") != "one-contract-production-resolver-batch"):
                raise evidence_records.EvidenceError("整组测试执行器或完整目标集合不匹配。")
            # Validate every human attestation before writing any successful state.
            for draft in drafts:
                approval_id = draft.get("review", {}).get("lawyer_approval_evidence_id")
                evidence_records.load(profile_dir, approval_id, draft, kind="human_approval")
            for draft in drafts:
                records[draft["client_rule_id"]] = evidence_records.write(profile_dir, draft,
                    kind="executed_regression", actor=actor, report=report)
        except evidence_records.EvidenceError as exc:
            raise ClientStoreError(str(exc)) from exc
        evidence_ids = {rule_id: record["evidence_id"] for rule_id, record in records.items()}
        updated = {}
        for draft in drafts:
            rule_id = draft["client_rule_id"]
            next_rule = {**draft, "approval_status": "regression_passed" if passed else "lawyer_approved", "revision": draft["revision"] + 1,
                         "updated_at": _now(), "updated_by": actor,
                         "review": {**draft["review"], "regression_evidence_ids": [evidence_ids[rule_id]]}}
            if not passed:
                next_rule["activation_status"] = "inactive"
            updated[rule_id] = _with_hash(next_rule)
        journal = _with_hash({"transaction_id": "vtx_" + uuid.uuid4().hex,
            "client_profile_id": profile_id, "target_set_sha256": target_hash, "actor": actor, "passed": passed,
            "before_sha256": {d["client_rule_id"]: d["content_sha256"] for d in drafts}, "drafts": updated})
        journal_path = paths.contained_path(profile_dir, "validation-commit.json")
        committed = False
        recovery_required = False
        try:
            _secure_write(journal_path, journal)
            committed = True
            self._recover_validation_commit(profile_dir)
        except Exception:
            # Replacement can commit before directory sync reports an error.
            if not committed and journal_path.is_file():
                committed = _read_json(journal_path).get("transaction_id") == journal["transaction_id"]
            if not committed:
                raise
            recovery_required = journal_path.is_file()
        return {"passed": passed, "evidence_ids": evidence_ids, "drafts": list(updated.values()),
                "release_check_recorded": passed, "transaction_id": journal["transaction_id"],
                "commit_status": "committed_recovery_pending" if recovery_required else "committed",
                "recovery_required": recovery_required}

    # ------------------------------------------------------------------ #
    # 发布 / 回滚 / 读取
    # ------------------------------------------------------------------ #
    @_transaction
    def publish_snapshot(
        self, profile_id: str, *,
        expected_active_snapshot: Optional[str],
        confirmation: bool,
        actor: str,
        enforce_single_active_version: bool = False,
        public_snapshot_id: str = "public_sha256_" + "0" * 64,
        public_asset_fingerprint: str = "sha256:" + "0" * 64,
        resolver_semantics_version: str = "0.1.0",
    ) -> Dict[str, Any]:
        if not confirmation:
            raise ClientStoreError("发布需要显式二次确认（RS-HC-004）。")
        if public_asset_fingerprint == "sha256:" + "0" * 64 or public_snapshot_id == "public_sha256_" + "0" * 64:
            from .public_repository import PublicRepository
            public = PublicRepository()
            public_snapshot_id = public.public_snapshot_id
            public_asset_fingerprint = public.public_asset_fingerprint
            resolver_semantics_version = public.resolver_semantics_version
        profile_dir = self._require_active_profile_dir(profile_id)
        pub = publisher.Publisher(profile_dir)

        drafts = self.list_drafts(profile_id)
        rules = []
        for draft in drafts:
            if draft.get("approval_status") not in PUBLISHABLE_APPROVAL_STATES:
                continue
            rule = dict(draft)
            rule["approval_status"] = "active"
            rule["activation_status"] = "active"
            rules.append(_with_hash(rule))

        if not rules:
            # 「没有可发布的规则」有两种截然不同的情形，原先一律拒绝，于是把第二种误伤了：
            #
            #   (a) 规则都被**停用**了（或本就没有规则）——这是合法的「以空快照停用」。
            #       CR-006 明确要求「禁止物理删除，**只能新快照停用**」；若这里也拒绝，
            #       **最后一条已发布规则将永远无法停用**——契约要求的能力被守卫堵死。
            #   (b) 有规则但都还没走完审批/回归——用户多半以为它们会被发布，
            #       这时必须拒绝并**指名**是哪些，否则会出现「按了发布却什么都没发布」的静默结果
            #       （LIFE-002 / LIFE-004 的本意）。
            pending = [d for d in drafts
                       if d.get("approval_status") not in PUBLISHABLE_APPROVAL_STATES
                       and d.get("approval_status") != "deprecated"]
            if pending:
                raise ClientStoreError(
                    "以下规则尚未完成律师批准与回归，不能发布（LIFE-002 / LIFE-004）："
                    + "、".join(str(d.get("client_rule_id")) for d in pending)
                )

        # 部分发布时，**如实回报哪些草稿没有被包含**。
        # 此前只在「一条可发布的都没有」时提示；混合情形（1 条已批准 + 1 条仍 candidate）
        # 会静默丢弃后者——不在生效集合、不在 excluded_rules、无冲突、无人工确认，
        # 用户看到的是「发布成功」却不知道少了一条。发布本身应当成功（发布已就绪的规则
        # 是正当流程），但结果不得静默。
        skipped = [
            {"client_rule_id": d.get("client_rule_id"),
             "approval_status": d.get("approval_status"),
             "reason": "尚未完成律师批准与回归" if d.get("approval_status") != "deprecated"
                       else "已停用"}
            for d in drafts
            if d.get("approval_status") not in PUBLISHABLE_APPROVAL_STATES
        ]

        if enforce_single_active_version:
            try:
                validator.ensure_single_active_version(rules)
            except validator.ValidationError as exc:
                raise ClientStoreError(str(exc)) from exc

        current_manifest = pub.read_manifest()
        current_id = current_manifest.get("active_snapshot_id")
        if current_id != expected_active_snapshot:
            raise ClientStoreError("expected active snapshot 与当前不一致；发布被拒绝（ACTIVE_SNAPSHOT_CONFLICT）。")
        current_rules = pub.load_snapshot(current_id)["rules_doc"]["rules"] if current_id else []
        target_set_sha256 = evidence_records.rule_set_hash(rules)
        require_rule_set = len(rules) > 1 and target_set_sha256 != evidence_records.rule_set_hash(current_rules)
        conflicts = guard_catalog.detect_conflicts(rules)
        if conflicts:
            raise ClientStoreError("拟发布规则之间存在静态冲突，请先处理冲突并完成整组测试（RULE_SET_CONFLICT）。")
        # Public dependencies are contextual, not a bag of every public ID. Only
        # the real resolver's successful applicability case can establish them.
        client_dependency_rules = [{**r, "depends_on": [d for d in r.get("depends_on", []) if str(d).startswith("cr_")]} for r in rules]
        missing = guard_catalog.check_dependencies(client_dependency_rules, set())
        if not missing.satisfied:
            raise ClientStoreError("客户规则依赖未纳入发布集合：" + "、".join(missing.missing))
        evidence_refs = []
        try:
            for rule in rules:
                evidence_refs.extend(evidence_records.publication_refs(profile_dir, rule,
                    target_set_sha256=target_set_sha256, require_rule_set=require_rule_set))
                if any(not str(d).startswith("cr_") for d in rule.get("depends_on", [])):
                    reports = [evidence_records.load(profile_dir, eid, rule, kind="executed_regression")["report"]
                               for eid in rule["review"]["regression_evidence_ids"]]
                    demonstrated = any(report["executor"]["name"] in ("one-contract-production-resolver", "one-contract-production-resolver-batch")
                        and any(case["passed"] and isinstance(case["actual"], dict)
                                and (case["actual"].get("applies") is True or rule["client_rule_id"] in case["actual"].get("rules", {})) for case in report["cases"])
                        for report in reports)
                    if not demonstrated:
                        raise evidence_records.EvidenceError("公共依赖须由实际适用场景测试证明，不能把全部公共编号视为可用。")
        except evidence_records.EvidenceError as exc:
            raise ClientStoreError(str(exc)) from exc

        try:
            snapshot = pub.publish(
                client_profile_id=profile_id,
                rules=rules,
                evidence_refs=evidence_refs,
                public_snapshot_id=public_snapshot_id,
                public_asset_fingerprint=public_asset_fingerprint,
                resolver_semantics_version=resolver_semantics_version,
                expected_active_snapshot=expected_active_snapshot,
                actor=actor,
            )
        except publisher.PublishError as exc:
            raise ClientStoreError(str(exc)) from exc

        # 把未包含的草稿随结果一并回报（见上文：混合发布不得静默丢弃）
        snapshot["skipped_drafts"] = skipped
        return snapshot

    @_transaction
    def create_restore_point(self, profile_id: str, *, actor: str,
                             public_snapshot_id: Optional[str] = None,
                             public_asset_fingerprint: Optional[str] = None,
                             resolver_semantics_version: Optional[str] = None) -> Dict[str, Any]:
        """Pin current immutable snapshot. Never clear an existing active set."""
        profile_dir = self._require_active_profile_dir(profile_id)
        pub = publisher.Publisher(profile_dir)
        pub.recover_pending_commits()
        current = pub.read_manifest()
        active_id = current.get("active_snapshot_id")
        if active_id:
            snapshot = pub.load_snapshot(active_id)["manifest"]
        else:
            if not all((public_snapshot_id, public_asset_fingerprint, resolver_semantics_version)):
                from .public_repository import PublicRepository
                public = PublicRepository()
                public_snapshot_id = public.public_snapshot_id
                public_asset_fingerprint = public.public_asset_fingerprint
                resolver_semantics_version = public.resolver_semantics_version
            snapshot = pub.publish(client_profile_id=profile_id, rules=[], evidence_refs=[],
                public_snapshot_id=public_snapshot_id, public_asset_fingerprint=public_asset_fingerprint,
                resolver_semantics_version=resolver_semantics_version, expected_active_snapshot=None,
                actor=actor, publish_reason="initial_empty_baseline")
        restore_id = "rp_" + uuid.uuid4().hex
        record = {"restore_point_id": restore_id, "snapshot_id": snapshot["snapshot_id"],
                  "client_profile_id": profile_id, "snapshot_sha256": snapshot["content_sha256"],
                  "created_by": actor, "created_at": _now()}
        _secure_write(paths.contained_path(profile_dir, "restore-points", restore_id + ".json"), record)
        audit.append_event(profile_dir, event_kind="restore_point_created", actor=actor, detail=record)
        return {**snapshot, "restore_point_id": restore_id, "active_changed": active_id is None}

    def publish_empty_snapshot(self, profile_id: str, *, actor: str) -> Dict[str, Any]:
        """Compatibility alias for the old baseline button; non-destructive."""
        return self.create_restore_point(profile_id, actor=actor)

    def load_active_snapshot(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """返回当前生效快照的 manifest；未发布时返回 None。"""
        profile_dir = self._require_profile_dir(profile_id)
        pub = publisher.Publisher(profile_dir)
        manifest = pub.read_manifest()
        snapshot_id = manifest.get("active_snapshot_id")
        if not snapshot_id:
            return None
        try:
            snapshot = pub.load_snapshot(snapshot_id)["manifest"]
            if snapshot["content_sha256"] != manifest.get("active_snapshot_sha256"):
                raise publisher.PublishError("active manifest 与快照内容哈希不一致。")
            return snapshot
        except publisher.PublishError as exc:
            raise SnapshotIntegrityError(str(exc)) from exc

    def load_snapshot(self, profile_id: str, snapshot_id: str) -> Dict[str, Any]:
        profile_dir = self._require_profile_dir(profile_id)
        pub = publisher.Publisher(profile_dir)
        try:
            doc = pub.load_snapshot(snapshot_id)
        except publisher.PublishError as exc:
            raise SnapshotIntegrityError(str(exc)) from exc
        return doc["manifest"]

    @_transaction
    def rollback_snapshot(
        self, profile_id: str, *,
        target_snapshot: str, expected_active_snapshot: Optional[str], actor: str,
    ) -> Dict[str, Any]:
        profile_dir = self._require_profile_dir(profile_id)
        pub = publisher.Publisher(profile_dir)
        try:
            return pub.rollback(
                target_snapshot=target_snapshot,
                expected_active_snapshot=expected_active_snapshot,
                actor=actor,
            )
        except publisher.PublishError as exc:
            raise ClientStoreError(str(exc)) from exc

    @_transaction
    def recover_pending_commits(self, profile_id: str) -> bool:
        profile_dir = self._require_profile_dir(profile_id)
        return publisher.Publisher(profile_dir).recover_pending_commits()

    def list_audit_events(self, profile_id: str) -> List[Dict[str, Any]]:
        profile_dir = self._require_profile_dir(profile_id)
        return audit.list_events(profile_dir)

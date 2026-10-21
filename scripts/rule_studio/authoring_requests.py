"""Private AI handoff requests, referencing (never replacing) authoring drafts.

All mutations lock requests before downstream repositories. A durable pending
operation with preallocated identifiers makes interrupted multi-file writes
recoverable, including adoption after a target was written but before its receipt.
"""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid
from one_contract_rules import models, paths, private_fs
from one_contract_rules.authoring_catalog import get_authoring_schema, derive_system_identity
from one_contract_rules.client_repository import _reset_review, RevisionConflict
from one_contract_rules.material_sources import MaterialSources
from one_contract_rules.transaction_lock import profile_lock
from .authoring_validation import inspect_draft


class RequestConflict(ValueError):
    """The human/AI is acting on an obsolete input or candidate."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, label, *, limit=200000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(label + "须为非空文本且长度不超过 " + str(limit) + " 字。")
    return value


class AuthoringRequests:
    def __init__(self, data_root, workspace, clients, proposals, public, *, read_only=False):
        self.root = Path(data_root) / "authoring-requests"
        self.workspace, self.clients, self.proposals, self.public = workspace, clients, proposals, public
        self.read_only = read_only
        self.sources = MaterialSources(data_root, read_only=read_only)
        if not read_only:
            self._safe_root()
            private_fs.secure_dir(self.root)

    def _safe_root(self):
        if self.root.is_symlink() or (hasattr(self.root, "is_junction") and self.root.is_junction()):
            raise ValueError("整理请求目录不能是链接。")

    def _path(self, request_id):
        self._safe_root()
        if not isinstance(request_id, str) or not re.fullmatch(r"ar_[a-f0-9]{32}", request_id):
            raise ValueError("整理请求编号无效。")
        return paths.contained_path(self.root, request_id + ".json")

    def _owner(self, owner):
        if owner is not None:
            if not isinstance(owner, str) or not owner:
                raise ValueError("请明确选择客户；公共请求使用空归属。")
            self.clients.get_client_profile(owner)

    def _writable(self):
        if self.read_only:
            raise ValueError("当前工作台为只读模式。")

    def _read(self, request_id, owner):
        self._owner(owner)
        record = json.loads(self._path(request_id).read_text(encoding="utf-8"))
        if record.get("request_id") != request_id or record.get("content_sha256") != models.content_hash(record):
            raise ValueError("整理请求完整性校验失败。")
        if record.get("client_profile_id") != owner:
            raise ValueError("整理请求与当前客户不一致。")
        return record

    def _write(self, record):
        record["content_sha256"] = models.content_hash(record)
        private_fs.atomic_write_json(self._path(record["request_id"]), record)

    @staticmethod
    def _input_hash(record):
        return models.content_hash({k: record[k] for k in ("intent", "intent_source_id", "materials", "context", "feedback")})

    @staticmethod
    def _check(record, revision):
        if type(revision) is not int or record["revision"] != revision:
            raise RequestConflict("整理请求已变化，请读取最新版本后重新核对。")

    def _view(self, record):
        view = copy.deepcopy(record)
        view.pop("pending", None)
        # Raw imports remain private and retrievable, without being copied to any
        # public proposal. The normal card reads only the latest explicit fields.
        candidate = view.get("candidate")
        view["candidate_valid"] = bool(candidate and candidate["input_sha256"] == view["input_sha256"] and not record.get("pending") and not record.get("candidate_invalidated_reason"))
        if candidate:
            current = self.workspace.get(candidate["draft_id"], client_profile_id=view["client_profile_id"])
            view["candidate_valid"] = view["candidate_valid"] and current["content_sha256"] == candidate["content_sha256"]
        view["runtime_enabled"] = False
        view["question_count"] = len(view.get("questions", []))
        if record.get("pending"):
            view["status"] = "recovery_pending"
        return view

    def create(self, *, intent, materials=None, context=None, client_profile_id=None, actor="local_operator"):
        self._writable(); self._owner(client_profile_id)
        _text(intent, "原始要求", limit=20000)
        materials = [] if materials is None else materials
        context = {} if context is None else context
        if not isinstance(materials, list) or len(materials) > 30 or not isinstance(context, dict):
            raise ValueError("材料须为列表（最多 30 项），上下文须为对象。")
        for material in materials:
            if not isinstance(material, dict):
                raise ValueError("每份材料须包含标题与原文。")
            _text(material.get("title"), "材料标题", limit=300); _text(material.get("text"), "材料原文")
        with profile_lock(self.root):
            original = self.sources.create(title="用户原始要求", text=intent, client_profile_id=client_profile_id)
            stored_materials = []
            for material in materials:
                source = self.sources.create(title=material["title"], text=material["text"], client_profile_id=client_profile_id)
                stored_materials.append({"source_id": source["source_id"], "title": source["title"], "text": source["text"], "sha256": source["content_sha256"]})
            now = _now()
            record = {"request_id": "ar_" + uuid.uuid4().hex, "client_profile_id": client_profile_id,
                      "title": intent.strip()[:80], "intent": intent, "intent_source_id": original["source_id"],
                      "materials": stored_materials, "context": copy.deepcopy(context), "feedback": [],
                      "revision": 1, "status": "awaiting_ai", "candidate": None, "questions": [], "source_map": [],
                      "summary": "", "validation": None, "imports": [], "candidate_history": [], "adopted": None,
                      "created_at": now, "updated_at": now, "created_by": actor, "updated_by": actor}
            record["input_sha256"] = self._input_hash(record)
            self._write(record)
            return self._view(record)

    def get(self, request_id, *, client_profile_id=None):
        return self._view(self._read(request_id, client_profile_id))

    def list(self, *, client_profile_id=None):
        self._owner(client_profile_id); self._safe_root()
        result = []
        for path in self.root.glob("ar_*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("client_profile_id") == client_profile_id:
                record = self.get(path.stem, client_profile_id=client_profile_id)
                result.append({k: record[k] for k in ("request_id", "client_profile_id", "title", "revision", "status", "updated_at", "candidate", "candidate_valid", "question_count")})
        return sorted(result, key=lambda r: r["updated_at"], reverse=True)

    def feedback(self, request_id, *, expected_revision, message, client_profile_id=None, actor="local_operator"):
        self._writable(); _text(message, "补充意见", limit=20000)
        with profile_lock(self.root):
            record = self._recover(self._read(request_id, client_profile_id)); self._check(record, expected_revision)
            if record.get("adopted"):
                raise RequestConflict("此整理结果已保存，请另建请求提出后续修改。")
            source = self.sources.create(title="用户补充意见", text=message, client_profile_id=client_profile_id)
            record["feedback"].append({"message": message, "source_id": source["source_id"], "at": _now(), "actor": actor})
            record.update(revision=record["revision"] + 1, status="feedback_pending", updated_at=_now(), updated_by=actor)
            record["input_sha256"] = self._input_hash(record)
            self._write(record)
            return self._view(record)

    def recover(self, request_id, *, expected_revision, client_profile_id=None, actor="local_operator"):
        """Resume only the operation already authorized and durably recorded."""
        self._writable()
        with profile_lock(self.root), profile_lock(self.workspace.root):
            record = self._read(request_id, client_profile_id)
            if not record.get("pending") and expected_revision == record.get("recovered_from_revision"):
                return self._view(record)
            self._check(record, expected_revision)
            if record.get("pending"):
                record["recovered_from_revision"] = expected_revision
                record = self._recover(record)
            return self._view(record)

    def _validate_links(self, record, questions, source_map):
        if not isinstance(questions, list) or not isinstance(source_map, list):
            raise ValueError("待确认事项和来源映射须为列表。")
        seen = set()
        for q in questions:
            if not isinstance(q, dict):
                raise ValueError("待确认事项须为对象。")
            _text(q.get("id"), "待确认事项编号", limit=100); _text(q.get("question"), "待确认问题", limit=5000)
            if q["id"] in seen or type(q.get("blocking")) is not bool:
                raise ValueError("待确认事项编号须唯一，并明确是否阻止保存。")
            seen.add(q["id"])
            if not isinstance(q.get("field_paths", []), list) or not all(isinstance(p, str) for p in q.get("field_paths", [])):
                raise ValueError("待确认字段路径须为字符串列表。")
        permitted = {record["intent_source_id"], *[m["source_id"] for m in record["materials"]], *[f["source_id"] for f in record["feedback"]]}
        for link in source_map:
            if not isinstance(link, dict) or link.get("source_id") not in permitted:
                raise ValueError("来源映射必须引用本请求已登记的原始材料。")
            source = self.sources.get(link["source_id"], client_profile_id=record["client_profile_id"])
            if not isinstance(link.get("field_paths"), list) or not all(isinstance(p, str) for p in link["field_paths"]):
                raise ValueError("来源映射字段路径须为字符串列表。")
            if "quote" in link and (not isinstance(link["quote"], str) or not link["quote"] or link["quote"] not in source["text"]):
                raise ValueError("来源摘录必须是对应原文中实际存在的完整片段。")

    def _normalize(self, kind, draft, owner, editor):
        body = copy.deepcopy(draft); adjustments = []
        properties = get_authoring_schema(kind)["schema"]["properties"]
        def set_managed(key, value, reason="由系统生成身份/版本信息；AI 候选不能携带人工审批或启用结论。"):
            if key not in body or body[key] != value:
                adjustments.append({"field_path": key, "before": copy.deepcopy(body.get(key)), "after": value, "reason": reason})
                body[key] = value
        set_managed("approval_status", "candidate"); set_managed("activation_status", "inactive")
        for key in ("reviewer", "reviewed_at", "legal_checked_at", "activated_at", "activation_basis"):
            if key in properties and "null" in properties[key].get("type", []):
                set_managed(key, None, "尚无实际审核行为，审核信息保持空值。")
            elif key in body:
                adjustments.append({"field_path": key, "before": body[key], "after": None, "reason": "实际审核记录须由后续真实操作产生。"})
                body.pop(key)
        if not body.get("version"):
            set_managed("version", "0.1.0" if owner is None else "1.0.0")
        if owner is None:
            wrapper = editor.get("proposal", editor)
            operation = wrapper.get("operation", "create") if isinstance(wrapper, dict) else "create"
            identity = {"principle": "doctrine_id", "clause_module": "module_id", "domain_principle": "domain_doctrine_id"}[kind]
            if kind != "domain_principle":
                current = body.get(identity)
                exists = any(item.get(identity) == current for item in self.public.index.all_nodes()) if current else False
                if not current or (operation == "create" and exists):
                    if operation == "supersede" and wrapper.get("target_rule_id"):
                        detail = self.public.get_rule_detail(wrapper["target_rule_id"])
                        original = detail.get("body") if detail else None
                        generated = original.get(identity) if isinstance(original, dict) else None
                    else:
                        generated = ("mod-" if kind == "clause_module" else "doctrine-") + uuid.uuid4().hex
                    if generated:
                        set_managed(identity, generated)
        if kind == "client_rule":
            if body.get("client_profile_id") not in (None, owner):
                raise ValueError("候选规则归属与整理请求客户不一致。")
            body["client_profile_id"] = owner
            if "review" in body:
                old = copy.deepcopy(body["review"])
                if not isinstance(old, dict):
                    raise ValueError("审核记录须为对象。")
                temp = {"review": old}; _reset_review(temp)
                body["review"] = temp["review"]
                if old != body["review"]:
                    adjustments.append({"field_path": "review", "before": old, "after": body["review"], "reason": "AI 不能代替真实人工审核和场景验证。"})
        else:
            derived = derive_system_identity(kind, body, skill_root=getattr(self.public, "_skill_root", None))
            for key in sorted(set(body) | set(derived)):
                if key not in body or key not in derived or body[key] != derived[key]:
                    adjustments.append({"field_path": key, "before": copy.deepcopy(body.get(key)), "after": copy.deepcopy(derived.get(key)),
                                        "reason": "依据本机实际知识目录生成归属编码与覆盖信息；无法核验的目录信息保持缺项。"})
            body = derived
        return body, adjustments

    def _candidate_validation(self, kind, body, editor, summary, owner):
        report = inspect_draft(kind, body, data_root=self.root.parent, client_profile_id=owner)
        def gap(path, label, message):
            report["issues"].append({"field_path": path, "label": label, "kind": "content", "message": message})
            report["content_ready"] = False
        if kind == "client_rule":
            intent = editor.get("condition_intent")
            condition = body.get("condition")
            clauses = condition.get("clauses", []) if isinstance(condition, dict) else []
            assessment = condition.get("assessment_mode", "structured") if isinstance(condition, dict) else "structured"
            if intent not in ("conditional", "unconditional", "semantic"):
                gap("editor_state.condition_intent", "适用条件的判断方式", "须明确由 AI 结合合同全文判断、填写结构化条件，或在适用范围内不另设条件；未知不能当作不限。")
            elif (intent == "semantic") != (assessment == "semantic"):
                gap("editor_state.condition_intent", "适用条件的判断方式", "填写状态须与规则的条件判断方式一致。")
            elif (intent == "conditional" and not clauses) or (intent == "unconditional" and clauses):
                gap("editor_state.condition_intent", "事实条件是否确定", "声明与实际条件不一致，请核对事实条件。")
            if editor.get("operation") not in (None, "create", "revise"):
                gap("editor_state.operation", "保存方式", "请选择新建，或明确修订既有客户草稿。")
            if editor.get("operation") == "revise":
                revision = editor.get("base_revision")
                if type(revision) is not int or revision < 1 or editor.get("target_rule_id") != body.get("client_rule_id"):
                    gap("editor_state.base_revision", "修订原草稿", "须保留原规则编号和真实原版本。")
        else:
            wrapper = editor.get("proposal", editor)
            if not isinstance(wrapper, dict):
                gap("editor_state.proposal", "公共建议说明", "须包含完整建议说明。")
                return report
            title = wrapper.get("title") or body.get("title") or body.get("name") or body.get("clause_group")
            rationale = wrapper.get("rationale") or summary
            if not isinstance(title, str) or not title.strip():
                gap("editor_state.proposal.title", "建议标题", "请概括规则或修改建议的名称。")
            if not isinstance(rationale, str) or not rationale.strip():
                gap("editor_state.proposal.rationale", "建议理由", "请说明为何提出此规则；不能直接代入私人材料原文。")
            sources = wrapper.get("source_refs", body.get("source_refs", body.get("source_ids", [])))
            if isinstance(sources, str):
                sources = [s.strip() for s in sources.split(",") if s.strip()]
            valid = {o["value"] for o in get_authoring_schema(kind)["vocabulary"]["source_options"]}
            if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or s not in valid for s in sources):
                gap("editor_state.proposal.source_refs", "公共依据来源", "须使用真实公共来源；私有原文只留在本次整理记录。")
            operation = wrapper.get("operation", "create"); target = wrapper.get("target_rule_id")
            if operation not in ("create", "supersede") or (operation == "create" and target) or (operation == "supersede" and (not target or self.public.get_rule(target) is None)):
                gap("editor_state.proposal.operation", "建议操作与原规则", "须明确新建或修改真实存在的公共规则。")
        return report

    def submit_result(self, request_id, *, expected_revision, input_sha256, kind, draft,
                      editor_state=None, summary="", questions=None, source_map=None, client_profile_id=None, actor="local_operator"):
        self._writable()
        if not isinstance(draft, dict) or (editor_state is not None and not isinstance(editor_state, dict)) or not isinstance(summary, str):
            raise ValueError("候选正文和编辑状态须为完整对象，整理说明须为文本。")
        if kind not in (("client_rule",) if client_profile_id is not None else ("domain_principle", "principle", "clause_module")):
            raise ValueError("候选种类与请求的客户/公共归属不一致。")
        questions = [] if questions is None else questions; source_map = [] if source_map is None else source_map
        with profile_lock(self.root):
            record = self._recover(self._read(request_id, client_profile_id)); self._check(record, expected_revision)
            if record.get("adopted") or input_sha256 != record["input_sha256"]:
                raise RequestConflict("原始要求已变化或已保存，请读取最新记录后重新整理。")
            self._validate_links(record, questions, source_map)
            body, adjustments = self._normalize(kind, draft, client_profile_id, editor_state or {})
            self.sources.validate_refs(body.get("source_refs"), client_profile_id=client_profile_id)
            self.sources.validate_refs(body.get("source_ids"), client_profile_id=client_profile_id)
            state = copy.deepcopy(editor_state or {})
            if client_profile_id is None and isinstance(state.get("proposal"), dict):
                for key in ("operation", "target_rule_id", "title", "rationale", "source_refs"):
                    if key in state["proposal"]:
                        state.setdefault(key, ", ".join(state["proposal"][key]) if key == "source_refs" and isinstance(state["proposal"][key], list) else state["proposal"][key])
            payload = {"kind": kind, "draft": body, "editor_state": state,
                       "summary": summary, "questions": copy.deepcopy(questions), "source_map": copy.deepcopy(source_map),
                       "managed_adjustments": adjustments, "actor": actor,
                       "raw_result": {"kind": kind, "draft": copy.deepcopy(draft), "editor_state": copy.deepcopy(editor_state or {}),
                                      "summary": summary, "questions": copy.deepcopy(questions), "source_map": copy.deepcopy(source_map),
                                      "input_sha256": input_sha256, "expected_revision": expected_revision}}
            payload["validation"] = self._candidate_validation(kind, body, state, summary, client_profile_id)
            record["pending"] = {"operation": "result", "draft_id": "aw_" + uuid.uuid4().hex, "payload": payload}
            self._write(record)
            return self._view(self._recover(record))

    def _recover(self, record):
        pending = record.get("pending")
        if not pending:
            return record
        if pending["operation"] == "result":
            data = pending["payload"]; draft_id = pending["draft_id"]
            if self.workspace._path(draft_id).exists():
                draft = self.workspace.get(draft_id, client_profile_id=record["client_profile_id"])
                if draft["draft"] != data["draft"] or draft["editor_state"] != data["editor_state"] or draft["revision"] != 1:
                    self._release_conflict(record, "中断的填写进度已被独立编辑；已保留新内容，请读取最新进度后重新整理。")
            else:
                base_revision = data["editor_state"].get("base_revision")
                if data["kind"] != "client_rule" or data["editor_state"].get("operation") != "revise" or type(base_revision) is not int or base_revision < 1:
                    base_revision = None
                draft = self.workspace.save(kind=data["kind"], draft=data["draft"], editor_state=data["editor_state"],
                                            client_profile_id=record["client_profile_id"], new_draft_id=draft_id, actor=data["actor"],
                                            base_revision=base_revision)
            if record.get("candidate"):
                record["candidate_history"].append(record["candidate"])
            validation = data["validation"]
            record["candidate"] = {k: draft[k] for k in ("kind", "draft_id", "revision", "content_sha256")}
            record["candidate"]["validation"] = validation
            record["candidate"]["input_sha256"] = record["input_sha256"]
            for key in ("questions", "source_map", "summary", "managed_adjustments"):
                record[key] = data[key]
            record["imports"].append({"at": _now(), "actor": data["actor"], "draft_id": draft_id, "raw_result": data["raw_result"]})
            record["validation"] = validation
            record.pop("candidate_invalidated_reason", None)
            record["status"] = "needs_review"
        elif pending["operation"] == "adopt":
            try:
                self._finish_adopt(record, pending)
            except (RequestConflict, RevisionConflict) as exc:
                # A different legitimate edit may have happened after the
                # request was approved, including while this process was down.
                # Retain the attempted transaction and release the stale CAS;
                # otherwise every future feedback/submit would be stuck retrying it.
                self._release_conflict(record, "目标草稿已另行修改；请读取最新草稿后重新整理。", detail=str(exc))
            record["status"] = "adopted"
        else:
            raise ValueError("整理请求存在未知待恢复操作。")
        record["revision"] += 1; record["updated_at"] = _now(); record["updated_by"] = pending["payload"]["actor"]
        record.pop("pending")
        self._write(record)
        return record

    def _release_conflict(self, record, reason, *, detail=None):
        record.setdefault("conflict_history", []).append({"at": _now(), "reason": detail or reason,
                                                          "transaction": copy.deepcopy(record["pending"])})
        record.pop("pending")
        record["candidate_invalidated_reason"] = reason
        record["status"] = "feedback_pending"
        record["revision"] += 1; record["updated_at"] = _now()
        self._write(record)
        raise RequestConflict(reason + "已保留冲突记录并解除旧保存，请读取最新整理请求后补充意见或重新提交候选。")

    def adopt(self, request_id, *, expected_revision, candidate_sha256, confirmation,
              client_profile_id=None, actor="local_operator"):
        self._writable()
        if confirmation is not True:
            raise ValueError("请明确核对并确认保存为待审核草稿。")
        with profile_lock(self.root), profile_lock(self.workspace.root):
            record = self._recover(self._read(request_id, client_profile_id))
            if record.get("adopted"):
                receipt = record["adoption_basis"]
                if candidate_sha256 == receipt["candidate_sha256"] and expected_revision in (receipt["request_revision"], record["revision"]):
                    return self._view(record)
                raise RequestConflict("该整理结果已保存，请读取已有草稿。")
            self._check(record, expected_revision)
            if not self._view(record)["candidate_valid"] or candidate_sha256 != record["candidate"]["content_sha256"]:
                raise RequestConflict("候选或原始要求已变化，请重新整理并核对。")
            if any(q["blocking"] for q in record["questions"]):
                raise ValueError("仍有待确认事项，请补充事实后让 AI 重新整理。")
            progress = self.workspace.get(record["candidate"]["draft_id"], client_profile_id=client_profile_id)
            validation = self._candidate_validation(progress["kind"], progress["draft"], progress["editor_state"], record["summary"], client_profile_id)
            if not validation["content_ready"]:
                raise ValueError("候选仍有内容缺项，不能保存为正式草稿。")
            payload = self._adoption_payload(record, progress, actor)
            record["pending"] = {"operation": "adopt", "payload": payload}
            record["adoption_basis"] = {"request_revision": expected_revision, "candidate_sha256": candidate_sha256}
            self._write(record)
            return self._view(self._recover(record))

    def _adoption_payload(self, record, progress, actor):
        body = copy.deepcopy(progress["draft"]); editor = progress["editor_state"]; owner = record["client_profile_id"]
        if owner is not None:
            revising = editor.get("operation") == "revise"
            revision = editor.get("base_revision") if revising else 0
            if revising:
                if type(revision) is not int or revision < 1 or body.get("client_rule_id") != editor.get("target_rule_id"):
                    raise ValueError("修改既有规则须明确原规则编号和原版本，不能推定覆盖。")
                existing = self.clients.get_draft(owner, body["client_rule_id"])
                if not existing or existing["revision"] != revision:
                    raise RequestConflict("原客户草稿已变化，请读取最新版本后重新整理。")
                published = self.clients._active_version_set(self.clients._require_profile_dir(owner), body["client_rule_id"])
                if body.get("version") in published:
                    raise ValueError("原规则版本已经发布；修改须提升版本号，保留原已启用快照。")
            else:
                body["client_rule_id"] = "cr_" + uuid.uuid4().hex; existing = None
            now = _now()
            body.update(schema_version="1.0", client_profile_id=owner, revision=revision + 1,
                        created_at=existing["created_at"] if existing else now, created_by=existing["created_by"] if existing else actor,
                        updated_at=now, updated_by=actor)
            body.setdefault("version", "1.0.0"); _reset_review(body)
            body["content_sha256"] = models.content_hash(body)
            # Validate before recording a pending commit, so correctable business
            # errors never leave a request trapped in a recovery transaction.
            self.clients._validate_rule_for_save(owner, body)
            return {"kind": "client_rule", "body": body, "expected_revision": revision, "actor": actor}
        wrapper = editor.get("proposal", editor)
        if not isinstance(wrapper, dict):
            raise ValueError("公共建议说明须为对象。")
        operation = wrapper.get("operation", "create")
        if operation not in ("create", "supersede"):
            raise ValueError("AI 整理保存支持新建或修改建议；不自动作出废止决定。")
        sources = wrapper.get("source_refs", body.get("source_refs", body.get("source_ids", [])))
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]
        valid_sources = {o["value"] for o in get_authoring_schema(progress["kind"])["vocabulary"]["source_options"]}
        if not isinstance(sources, list) or not sources or any(s not in valid_sources for s in sources):
            raise ValueError("公共建议须选择真实公共来源，私人材料不能进入公开来源。")
        title = wrapper.get("title") or body.get("title") or body.get("name") or body.get("clause_group")
        rationale = wrapper.get("rationale") or record["summary"]
        _text(title, "公共建议标题", limit=200); _text(rationale, "公共建议理由", limit=5000)
        target = wrapper.get("target_rule_id")
        if (operation == "create" and target) or (operation == "supersede" and (not target or self.public.get_rule(target) is None)):
            raise ValueError("公共建议操作与原规则不一致。")
        return {"kind": "public_proposal", "proposal_id": "pr_" + uuid.uuid4().hex[:12], "actor": actor,
                "arguments": {"operation": operation, "layer": "domain" if progress["kind"] == "domain_principle" else "type",
                              "target_rule_id": target, "title": title, "rationale": rationale, "source_refs": sources, "proposed_rule": body}}

    def _finish_adopt(self, record, pending):
        data = pending["payload"]; owner = record["client_profile_id"]
        if data["kind"] == "client_rule":
            body = data["body"]; rule_id = body["client_rule_id"]
            existing = self.clients.get_draft(owner, rule_id)
            # The client repository owns timestamps/hashes and resets reviews.
            # Match the complete business payload and expected resulting revision,
            # never only a hash computed before that legitimate normalization.
            ignored = {"content_sha256", "updated_at", "approval_status", "activation_status", "review"}
            if existing and existing.get("revision") == data["expected_revision"] + 1 and {k: v for k, v in existing.items() if k not in ignored} == {k: v for k, v in body.items() if k not in ignored}:
                saved = existing
            elif (existing and existing.get("revision") != data["expected_revision"]) or (not existing and data["expected_revision"]):
                raise RequestConflict("客户草稿在保存期间已修改或删除，旧版本不能覆盖。")
            elif data["expected_revision"]:
                saved = self.clients.save_draft(owner, body, expected_revision=data["expected_revision"], actor=data["actor"])
            else:
                saved = self.clients.create_new_rule(owner, body, actor=data["actor"])
            record["adopted"] = {"kind": "client_rule", "client_profile_id": owner, "client_rule_id": rule_id, "content_sha256": saved["content_sha256"]}
        else:
            proposal_id = data["proposal_id"]
            if self.proposals._proposal_path(proposal_id).exists():
                saved = self.proposals.get_proposal(proposal_id)
                if any(saved.get(k) != v for k, v in data["arguments"].items()):
                    raise RequestConflict("中断保存的公共建议已被修改，不能覆盖。")
            else:
                saved = self.proposals.create_proposal(new_proposal_id=proposal_id, actor=data["actor"], **data["arguments"])
            record["adopted"] = {"kind": "public_proposal", "client_profile_id": None, "proposal_id": proposal_id, "content_sha256": saved["content_sha256"]}

"""Save incomplete authoring progress outside executable rule repositories."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid
from one_contract_rules import models, paths, private_fs
from one_contract_rules.transaction_lock import profile_lock
from .authoring_validation import inspect_draft


class AuthoringWorkspace:
    def __init__(self, data_root, *, read_only=False):
        self.root = Path(data_root) / "authoring-workspaces"
        if self.root.is_symlink() or (hasattr(self.root, 'is_junction') and self.root.is_junction()):
            raise ValueError("填写进度目录不能是符号链接。")
        self.read_only = read_only
        if not read_only:
            private_fs.secure_dir(self.root)

    def _path(self, draft_id):
        if self.root.is_symlink() or (hasattr(self.root, 'is_junction') and self.root.is_junction()):
            raise ValueError("填写进度目录不能是链接或重解析点。")
        if not isinstance(draft_id, str) or not re.fullmatch(r"aw_[a-f0-9]{32}", draft_id):
            raise ValueError("填写进度编号无效。")
        return paths.contained_path(self.root, draft_id + ".json")

    def get(self, draft_id, *, client_profile_id=None):
        record = json.loads(self._path(draft_id).read_text(encoding="utf-8"))
        if record.get("content_sha256") != models.content_hash(record) or record["draft_id"] != draft_id:
            raise ValueError("填写进度完整性校验失败。")
        if record.get("client_profile_id") != client_profile_id:
            raise ValueError("填写进度与当前选中客户不一致。")
        return record

    def list(self, *, client_profile_id=None):
        if not self.root.exists():
            return []
        records = []
        for path in self.root.glob("aw_*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("client_profile_id") == client_profile_id:
                record = self.get(path.stem, client_profile_id=client_profile_id)
                records.append({k: record[k] for k in ("draft_id", "kind", "title", "revision", "updated_at", "validation")})
        return sorted(records, key=lambda r: r["updated_at"], reverse=True)

    def save(self, *, kind, draft, client_profile_id=None, draft_id=None,
             expected_revision=0, base_revision=None, editor_state=None, actor, new_draft_id=None):
        if self.read_only:
            raise ValueError("当前工作台为只读模式。")
        if not isinstance(draft, dict):
            raise ValueError("填写进度须包含规则内容。")
        if editor_state is not None and not isinstance(editor_state, dict):
            raise ValueError("填写界面状态必须是对象。")
        if base_revision is not None and (type(base_revision) is not int or base_revision < 1):
            raise ValueError("原始规则版本无效。")
        if kind == "client_rule" and (not client_profile_id or draft.get("client_profile_id") != client_profile_id):
            raise ValueError("请先明确选择客户。")
        if kind != "client_rule" and client_profile_id is not None:
            raise ValueError("公共规则填写进度不应混入客户归属。")
        validation = inspect_draft(kind, draft, data_root=self.root.parent, client_profile_id=client_profile_id)
        with profile_lock(self.root):
            if new_draft_id is not None:
                if draft_id is not None or self._path(new_draft_id).exists():
                    raise ValueError("新填写进度编号已被使用，不能覆盖。")
            existing = self.get(draft_id, client_profile_id=client_profile_id) if draft_id else None
            if existing and existing['kind'] != kind:
                raise ValueError("同一填写进度不能更换规则种类；请另存为新进度。")
            if type(expected_revision) is not int or expected_revision != (existing["revision"] if existing else 0):
                raise ValueError("填写进度已被其他操作修改，请重新载入。")
            draft_id = draft_id or new_draft_id or "aw_" + uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            record = {"draft_id": draft_id, "kind": kind, "client_profile_id": client_profile_id,
                      "title": draft.get("title") or draft.get("name") or draft.get("clause_group") or "未命名填写进度",
                      "draft": copy.deepcopy(draft), "base_revision": base_revision,
                      "editor_state": copy.deepcopy(editor_state or {}),
                      "revision": expected_revision + 1, "updated_at": now, "updated_by": actor,
                      "created_at": existing["created_at"] if existing else now,
                      "validation": validation, "runtime_enabled": False}
            record["content_sha256"] = models.content_hash(record)
            private_fs.atomic_write_json(self._path(draft_id), record)
            return record

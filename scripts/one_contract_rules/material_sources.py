"""Immutable, owner-bound original materials; never part of public knowledge."""
from __future__ import annotations
import json
import re
import uuid
from pathlib import Path
from . import models, paths, private_fs


class MaterialSources:
    def __init__(self, data_root, *, read_only=False):
        self.parent = Path(data_root) / "authoring-requests"
        self.root = self.parent / "sources"
        self.read_only = read_only
        if not read_only:
            for path in (self.parent, self.root):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise ValueError("私有材料目录不能是链接。")
            private_fs.secure_dir(self.root)

    def _path(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r"src-local-[a-f0-9]{32}", source_id):
            raise ValueError("私有材料来源编号无效。")
        for path in (self.parent, self.root):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("私有材料目录不能是链接。")
        return paths.contained_path(self.root, source_id + ".json")

    def create(self, *, title, text, client_profile_id):
        if self.read_only:
            raise ValueError("当前工作台为只读模式。")
        if not isinstance(title, str) or not title.strip() or not isinstance(text, str) or not text.strip():
            raise ValueError("原始材料须包含标题和原文。")
        source_id = "src-local-" + uuid.uuid4().hex
        record = {"source_id": source_id, "title": title, "text": text,
                  "client_profile_id": client_profile_id, "private": True}
        record["content_sha256"] = models.content_hash(record)
        private_fs.atomic_write_json(self._path(source_id), record)
        return record

    def get(self, source_id, *, client_profile_id):
        record = json.loads(self._path(source_id).read_text(encoding="utf-8"))
        if record.get("content_sha256") != models.content_hash(record) or record.get("source_id") != source_id:
            raise ValueError("私有材料来源完整性校验失败。")
        if record.get("client_profile_id") != client_profile_id:
            raise ValueError("私有材料不属于当前客户，不能跨客户引用。")
        return record

    def options(self, *, client_profile_id):
        if client_profile_id is None or not self.root.exists():
            return []
        options = []
        for path in self.root.glob("src-local-*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("client_profile_id") == client_profile_id:
                record = self.get(path.stem, client_profile_id=client_profile_id)
                options.append({"value": record["source_id"], "label": record["title"] + "（本客户私有材料）"})
        return options

    def validate_refs(self, refs, *, client_profile_id):
        for ref in refs if isinstance(refs, list) else []:
            if isinstance(ref, str) and ref.startswith("src-local-"):
                if client_profile_id is None:
                    raise ValueError("私有材料不能用作公共规则的公开来源。")
                self.get(ref, client_profile_id=client_profile_id)

"""Execute local checks for a complete public proposal without changing assets.

This is schema/content/reference/dependency validation. It does not execute
public resolver goldens, legal research, DOCX delivery or deployment checks.
"""
from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Mapping
from jsonschema import Draft202012Validator, FormatChecker

from . import fingerprint, models, paths

VALIDATOR_VERSION = "1.0.0"
SCHEMAS = {
    "clause_module": ("module_id", "clause_module.schema.json", "type"),
    "principle": ("doctrine_id", "principle_catalog.schema.json", "type"),
    "domain_principle": ("domain_doctrine_id", "domain_principle.schema.json", "domain"),
    "knowledge_source": ("source_id", "knowledge_source.schema.json", "type"),
    "trigger": ("trigger_id", "equity_trigger.schema.json", "type"),
    "experience_card": ("experience_id", "experience_card.schema.json", "type"),
}
GLOBAL_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["document_id", "markdown"], "properties": {
        "document_id": {"type": "string", "pattern": r"^references/[a-z0-9][a-z0-9-]*\.md$"},
        "markdown": {"type": "string", "minLength": 1}}}


def subject_hash(proposal):
    return models.content_hash({key: value for key, value in proposal.items()
        if key not in ("content_sha256", "status", "created_at", "created_by")}, exclude=())


def current_fingerprint(public_repository):
    root = getattr(public_repository, "_skill_root", paths.skill_root())
    return fingerprint.fingerprint_public_assets(skill_root=root).public_asset_fingerprint


def _kind(body):
    if isinstance(body, Mapping):
        if "document_id" in body:
            return "global_document"
        for kind, (identity, _, _) in SCHEMAS.items():
            if identity in body:
                return kind
    return None


def execute(proposal, public_repository):
    """Return observed check results; callers cannot supply a passed flag."""
    checks = {name: [] for name in ("public_baseline", "complete_content", "schema", "references", "dependencies")}
    dependency_notes = []
    def issue(check, field, message):
        checks[check].append({"field_path": field, "message": message})
    baseline = current_fingerprint(public_repository)
    if baseline != proposal["base_public_fingerprint"] or baseline != public_repository.public_asset_fingerprint:
        issue("public_baseline", "base_public_fingerprint", "公共规则已变化，请重新载入并核对候选内容。")
    operation = proposal["operation"]
    target_id = proposal.get("target_rule_id")
    target = public_repository.get_rule(target_id) if target_id else None
    detail = public_repository.get_rule_detail(target_id, projection_mode="maintenance") if target else None
    original = (detail or {}).get("body")
    body = copy.deepcopy(proposal.get("proposed_rule"))
    kind = "global_document" if target and target.node_kind == "global_document" else _kind(body)
    if operation == "deprecate":
        kind = "global_document" if isinstance(original, str) else _kind(original)
        body = original
    if operation != "create" and target is None:
        issue("references", "target_rule_id", "修改目标不存在。")
    if target and target.layer != proposal["layer"]:
        issue("references", "layer", "目标所在层级与建议不一致。")
    if operation != "deprecate":
        if kind == "global_document":
            schema = GLOBAL_SCHEMA
            if proposal["layer"] != "global":
                issue("references", "layer", "全局文档必须属于全局层。")
            if target_id and isinstance(body, Mapping) and body.get("document_id") != target_id:
                issue("references", "proposed_rule.document_id", "候选文档必须保持修改目标编号。")
            if operation == "create" and isinstance(body, Mapping) and public_repository.get_rule(body.get("document_id")):
                issue("references", "proposed_rule.document_id", "该全局文档已存在，请使用修改建议。")
        elif kind in SCHEMAS:
            identity, schema_file, layer = SCHEMAS[kind]
            root = getattr(public_repository, "_skill_root", paths.skill_root())
            schema = json.loads((Path(root) / "assets/knowledge_v2/schemas" / schema_file).read_text(encoding="utf-8"))
            if proposal["layer"] != layer:
                issue("references", "layer", "规则种类与所属层级不一致。")
            if isinstance(original, Mapping):
                if body.get(identity) != original.get(identity) or (kind == "principle" and body.get("type_id") != original.get("type_id")):
                    issue("references", "proposed_rule." + identity, "修改建议必须保留目标规则身份与类型映射；新身份应另建建议。")
                old, new = original.get("version"), body.get("version")
                if old and (not isinstance(new, str) or not re.fullmatch(r"\d+\.\d+\.\d+", new)
                            or tuple(map(int, new.split("."))) <= tuple(map(int, old.split(".")))):
                    issue("complete_content", "proposed_rule.version", "修改后的候选版本必须高于公共原版本。")
            if operation == "create":
                identity_value = body.get(identity)
                existing = [item for item in public_repository.index.all_nodes() if item.get(identity) == identity_value
                            and (kind != "principle" or item.get("type_id") == body.get("type_id"))]
                if existing:
                    issue("references", "proposed_rule." + identity, "该规则身份已存在，请使用修改建议。")
        else:
            schema = None
            issue("schema", "proposed_rule", "缺少完整规则身份，或该规则种类尚无可执行的结构契约。")
        if schema is not None:
            for error in sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(body), key=lambda e: str(list(e.path))):
                issue("schema", "proposed_rule" + ("." + ".".join(map(str, error.path)) if error.path else ""), error.message)
        if kind in ("clause_module", "principle", "domain_principle") and isinstance(body, Mapping) and not checks["schema"]:
            # Shared authoring checks distinguish actual business gaps from
            # governance metadata. Their own source lookups are supplemented by
            # the exact repository reference checks below.
            from rule_studio.authoring_validation import inspect_draft
            inspected = inspect_draft(kind, body, public_repository=public_repository)
            for problem in inspected["issues"]:
                if problem["kind"] == "content":
                    issue("complete_content", "proposed_rule." + problem["field_path"], problem["message"])
        if kind == "global_document" and isinstance(body, Mapping) and not str(body.get("markdown", "")).strip():
            issue("complete_content", "proposed_rule.markdown", "须保存完整的非空候选正文。")
    else:
        incoming = public_repository.get_rule_relationships(target_id)["incoming"] if target else []
        for edge in incoming:
            issue("dependencies", "target_rule_id", "停用目标仍被引用：" + edge["from_id"] + "（" + edge["source_field"] + "）。")

    index = public_repository.index
    source_ids = {s["source_id"] for s in index.sources}
    module_by_id = {m["module_id"]: m for m in index.modules}
    doctrine_by_id = defaultdict(list)
    for item in index.type_principles:
        doctrine_by_id[item["doctrine_id"]].append(item)
    type_to_domain = index.type_to_domain
    taxonomy = {item["type_id"]: item for item in index.taxonomy.get("catalog_types", [])}
    for source in proposal.get("source_refs", []):
        if source not in source_ids:
            issue("references", "source_refs", "来源未登记：" + str(source))
    if isinstance(body, Mapping) and not checks["schema"]:
        for source in body.get("source_ids", []) if isinstance(body.get("source_ids"), list) else []:
            if source not in source_ids:
                issue("references", "proposed_rule.source_ids", "来源未登记：" + str(source))
        types = body.get("type_ids") if isinstance(body.get("type_ids"), list) else ([body["type_id"]] if body.get("type_id") else [])
        for type_id in types:
            if type_id not in taxonomy:
                issue("references", "proposed_rule.type_ids", "合同类型未登记：" + str(type_id))
        if kind == "clause_module" and body.get("type_ids") and body.get("type_id") not in types:
            issue("references", "proposed_rule.type_id", "主要合同类型必须在完整适用类型清单中。")
        if body.get("type_codes") and set(body["type_codes"]) != {taxonomy[t]["type_code"] for t in types if t in taxonomy}:
            issue("references", "proposed_rule.type_codes", "类型编码清单必须与适用合同类型一一对应。")
        if body.get("type_id") in taxonomy and body.get("type_code") and body["type_code"] != taxonomy[body["type_id"]]["type_code"]:
            issue("references", "proposed_rule.type_code", "合同类型编号与目录编码不一致。")
        for field in ("linked_module_ids", "derived_module_ids", "required_modules"):
            for ref in body.get(field, []) if isinstance(body.get(field), list) else []:
                if ref not in module_by_id:
                    issue("references", "proposed_rule." + field, "模块未登记：" + str(ref))
                elif field == "linked_module_ids" and body.get("type_id") not in _types(module_by_id[ref]):
                    issue("references", "proposed_rule." + field, "关联模块不适用于当前合同类型：" + str(ref))
        for ref in body.get("derived_principle_ids", []) if isinstance(body.get("derived_principle_ids"), list) else []:
            if ref not in doctrine_by_id or not any(type_to_domain.get(d["type_id"]) == body.get("domain_code") for d in doctrine_by_id[ref]):
                issue("references", "proposed_rule.derived_principle_ids", "原则未登记或不属于本业务大类：" + str(ref))
        if kind == "domain_principle" and not checks["schema"]:
            code = body["domain_code"]
            domain_types = {t for t, domain in type_to_domain.items() if domain == code and t in taxonomy}
            coverage = body["coverage"]
            supported, unsupported = set(coverage["supported_type_ids"]), set(coverage["unsupported_type_ids"])
            if supported & unsupported or supported | unsupported != domain_types or coverage["total_type_count"] != len(domain_types) or coverage["supported_type_count"] != len(supported):
                issue("references", "proposed_rule.coverage", "覆盖范围及计数必须与真实业务大类目录一致。")
            if body["domain_doctrine_id"] != "domain-doctrine-" + code.lower():
                issue("references", "proposed_rule.domain_doctrine_id", "大类原则身份与大类编码不一致。")
            indexed = {ref for entry in body["module_index"] for ref in entry["module_ids"]}
            if indexed != set(body["derived_module_ids"]):
                issue("references", "proposed_rule.module_index", "模块索引与关联模块清单不一致。")
        if kind == "clause_module" and not checks["schema"] and operation != "deprecate":
            module_by_id[body["module_id"]] = body
            _check_dependencies(body, module_by_id, issue, dependency_notes)

    if current_fingerprint(public_repository) != baseline:
        issue("public_baseline", "base_public_fingerprint", "检查过程中公共资产发生变化，本次结果不可用于确认。")
    observations = [{"check_id": name, "passed": not problems, "issues": problems} for name, problems in checks.items()]
    return {"validator": {"name": "one-contract-public-proposal-checks", "version": VALIDATOR_VERSION},
            "scope": "public_proposal_structure_references_dependencies", "subject_sha256": subject_hash(proposal),
            "public_asset_fingerprint": baseline, "candidate_sha256": models.content_hash({"candidate": proposal.get("proposed_rule")}, exclude=()),
            "checks": observations, "passed": all(item["passed"] for item in observations),
            "dependency_notes": dependency_notes,
            "unverified_source_family_refs": list(body.get("source_family_ids") or []) if isinstance(body, Mapping) and isinstance(body.get("source_family_ids"), list) else [],
            "unexecuted_checks": ["selector_equivalence", "resolver_golden", "hard_boundary", "export_privacy", "docx_delivery"],
            "effective": False, "notice": "仅执行正文完整性、结构、引用和依赖检查；未执行法律审核、解析回归或合同交付验收，公共资产未启用。"}


def _types(module):
    return set(module.get("type_ids") or ([module["type_id"]] if module.get("type_id") else []))


def _check_dependencies(body, modules, issue, notes):
    own_id = body["module_id"]
    own_types = _types(body)
    mapping = body.get("dependencies_by_type")
    if mapping is not None and set(mapping) != own_types:
        issue("dependencies", "proposed_rule.dependencies_by_type", "逐类型依赖必须完整覆盖且仅覆盖本模块的适用类型。")
    if mapping is not None and set(body.get("dependencies", [])) != {r for refs in mapping.values() for r in refs}:
        issue("dependencies", "proposed_rule.dependencies", "依赖汇总必须与逐类型依赖的并集一致。")
    def dependencies(module, context_type):
        scoped = module.get("dependencies_by_type")
        return scoped.get(context_type, []) if isinstance(scoped, Mapping) else module.get("dependencies", [])
    for context_type in own_types:
        visited = set()
        def visit(current, stack):
            if current in stack:
                # Public modules describe mutually required companion content;
                # the selector does not define a topological execution order.
                notes.append({"type_id": context_type, "mutual_dependencies": stack[stack.index(current):] + [current],
                              "requires_runtime_validation": True})
                return
            if current in visited:
                return
            visited.add(current)
            for ref in dependencies(modules[current], context_type):
                if ref == current:
                    issue("dependencies", "proposed_rule.dependencies", "模块不能以前置依赖引用自身：" + ref)
                elif ref not in modules:
                    issue("dependencies", "proposed_rule.dependencies", "前置模块未登记：" + str(ref))
                elif context_type not in _types(modules[ref]):
                    issue("dependencies", "proposed_rule.dependencies_by_type", "依赖模块不适用于 " + context_type + "：" + str(ref))
                else:
                    visit(ref, stack + [current])
        visit(own_id, [])

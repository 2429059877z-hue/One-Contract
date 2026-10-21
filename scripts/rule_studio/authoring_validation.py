"""Report authored-content gaps independently from future approval records."""
from __future__ import annotations
from typing import Mapping
from jsonschema import Draft202012Validator, FormatChecker
from one_contract_rules import authoring_catalog, validator


def _has_content(value):
    """Required business text must contain text, not a nonempty container."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value) and all(_has_content(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_has_content(v) for v in value)
    # False and zero are legitimate typed business values.
    return value is not None


def inspect_draft(kind: str, body: Mapping, *, public_repository=None, data_root=None, client_profile_id=None) -> dict:
    if not isinstance(body, Mapping):
        raise ValueError("规则内容必须是结构化对象。")
    meta = authoring_catalog.get_authoring_schema(kind)
    fields = {f["path"]: f for f in meta["fields"]}
    issues = []
    for error in sorted(Draft202012Validator(meta["schema"], format_checker=FormatChecker()).iter_errors(body), key=lambda e: str(list(e.path))):
        path = ".".join(str(k) for k in error.path)
        affected = path.split(".")[0]
        missing = [k for k in error.validator_value if k not in error.instance] if error.validator == "required" else []
        if missing:
            for key in missing:
                field_path = path + "." + key if path else key
                field = fields.get(field_path, {})
                issues.append({"field_path": field_path, "label": field.get("label", key),
                               "kind": "governance" if field.get("managed_by") in ("system", "reviewer") else "content",
                               "message": "缺少“" + field.get("label", key) + "”。"})
        else:
            field = fields.get(path, fields.get(affected, {}))
            issues.append({"field_path": path, "label": field.get("label", path),
                           "kind": "governance" if field.get("managed_by") in ("system", "reviewer") else "content",
                           "message": error.message})
    source_ids = set(o["value"] for o in meta["vocabulary"].get("source_options", []))
    if kind == "client_rule" and data_root is not None and client_profile_id is not None:
        from one_contract_rules.material_sources import MaterialSources
        source_ids.update(o["value"] for o in MaterialSources(data_root, read_only=True).options(client_profile_id=client_profile_id))
    for key in ("source_refs", "source_ids"):
        for value in body.get(key, []) if isinstance(body.get(key), list) else []:
            if not isinstance(value, str) or value not in source_ids:
                issues.append({"field_path": key, "label": "依据来源", "kind": "content", "message": "来源未登记：" + str(value)})
    if meta["kind"] == "client_rule" and all(body.get(k) is None or isinstance(body.get(k), Mapping) for k in ('scope', 'effect', 'condition', 'validity')):
        try:
            validator.validate_rule_business(body)
        except validator.ValidationError as exc:
            issues.append({"field_path": exc.field_path, "label": "业务约束", "kind": "content", "message": str(exc)})
    # Schemas intentionally allow these to be empty during migration. Surface
    # them as substantive gaps instead of claiming a production-ready rule.
    meaningful = {"domain_principle": ["review_objectives", "judgment_coordinates", "hard_boundaries", "required_evidence"],
                  "principle": ["review_objectives", "action_guidance", "hard_boundaries"],
                  "clause_module": ["review_objective", "review_checks", "variants"],
                  "client_rule": ["title", "effect", "source_refs"]}[meta["kind"]]
    for key in meaningful:
        value = body.get(key)
        if key == 'effect' and isinstance(value, Mapping):
            value = {k: value.get(k) for k in ('action', 'target_key', 'value')}
        empty = not _has_content(value)
        if empty and not any(i["field_path"] == key for i in issues):
            issues.append({"field_path": key, "label": fields.get(key, {}).get("label", key), "kind": "content", "message": "尚无完整业务内容，请补充或说明不适用的理由。"})
    return {"kind": meta["kind"], "schema_valid": not list(Draft202012Validator(meta["schema"], format_checker=FormatChecker()).iter_errors(body)),
            "content_ready": not any(i["kind"] == "content" for i in issues),
            "issues": issues, "runtime_enabled": False,
            "notice": "字段检查不代替法律审核、实际场景测试或发布批准；草稿始终未启用。"}

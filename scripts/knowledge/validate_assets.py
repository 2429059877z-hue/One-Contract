#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
import argparse
import json
import re


ALLOWED_STATUSES = {"candidate", "lawyer_approved", "regression_passed", "active", "deprecated"}
TEXT_EXTENSIONS = {".md", ".json", ".py", ".yaml", ".yml", ".txt", ".csv"}
MODULE_REQUIRED = {
    "module_id", "version", "type_id", "clause_group", "review_objective", "failure_mode",
    "roles", "scene_tags", "requiredness", "assumptions", "placeholders", "variants",
    "dependencies", "conflicts", "source_ids", "source_family_ids", "approval_status",
    "reviewer", "reviewed_at", "legal_checked_at",
}
GROUP_REQUIRED = {
    "module_group_id", "version", "type_id", "name", "member_module_ids",
    "review_sequence", "joint_failure_modes", "consistency_checks", "approval_status",
    "activation_status",
}
TEMPLATE_REQUIRED = {
    "template_id", "version", "type_id", "type_code", "name", "relative_path",
    "document_kind", "sample_support_status", "approval_status", "activation_status",
}
SOURCE_REQUIRED = {
    "source_id", "source_kind", "title", "author", "jurisdiction",
    "published_at", "verified_at", "knowledge_tags", "used_for", "approval_status",
}
TRIGGER_REQUIRED = {
    "trigger_id", "version", "type_id", "name", "severity", "hard_signals",
    "false_positive_guards", "evidence_state", "human_confirmation_required",
    "auto_action", "required_modules", "required_outputs", "prohibited_outputs",
    "source_ids", "approval_status", "activation_status",
}
EXPERIENCE_REQUIRED = {
    "experience_id", "type_id", "title", "source_ids", "source_status",
    "observation", "review_rule", "limits", "approval_status", "activation_status",
}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _belongs_to_active_type(item: Mapping[str, Any], active_types: set[str]) -> bool:
    """Return True if an asset serves any activated type."""
    type_ids = item.get("type_ids")
    if isinstance(type_ids, list):
        return bool(set(type_ids) & active_types)
    return bool(item.get("type_id") in active_types)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate modular knowledge assets and privacy boundaries.")
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    root = args.skill_root.resolve()
    assets = root / "assets/knowledge_v2"
    errors: list[str] = []
    manifest = load(assets / "asset_manifest.json")
    if manifest.get("runtime_eligible_status") != "active":
        errors.append("runtime_eligible_status must be active")
    active_types = set(manifest.get("active_type_ids", []))
    modules = load(assets / "module_catalog.json").get("assets", [])
    principles = load(assets / "principle_catalog.json").get("assets", [])
    groups = load(assets / "module_group_catalog.json").get("assets", [])
    templates = load(assets / "template_catalog.json").get("assets", [])
    sources = load(assets / "source_catalog.json").get("assets", [])
    triggers = load(assets / "trigger_catalog.json").get("assets", [])
    experiences = load(assets / "experience_catalog.json").get("assets", [])
    module_ids = {item.get("module_id") for item in modules}
    source_ids = {item.get("source_id") for item in sources}
    if len(source_ids) != len(sources):
        errors.append("source ids must be unique")
    for module in modules:
        missing = MODULE_REQUIRED - set(module)
        if missing:
            errors.append(f"module {module.get('module_id')} missing {sorted(missing)}")
        if module.get("approval_status") not in ALLOWED_STATUSES:
            errors.append(f"module {module.get('module_id')} has invalid status")
        if module.get("approval_status") == "active" and not all(
            module.get(key) for key in ("reviewer", "reviewed_at", "legal_checked_at")
        ):
            errors.append(f"active module {module.get('module_id')} lacks approval evidence")
        if module.get("approval_status") != "active" and module.get("activation_status") != "inactive":
            errors.append(f"non-active module {module.get('module_id')} must remain inactive")
        if set(module.get("dependencies_by_type", {})) != set(module.get("type_ids", [])):
            errors.append(f"module {module.get('module_id')} dependency map/type_ids mismatch")
        if not set(module.get("source_ids", [])).issubset(source_ids):
            errors.append(f"module {module.get('module_id')} references unknown source")
    for principle in principles:
        if principle.get("approval_status") not in ALLOWED_STATUSES:
            errors.append(f"principle {principle.get('doctrine_id')} has invalid status")
        if principle.get("approval_status") != "active" and principle.get("activation_status") != "inactive":
            errors.append(f"non-active principle {principle.get('doctrine_id')} must remain inactive")
        if not set(principle.get("source_ids", [])).issubset(source_ids):
            errors.append(f"principle {principle.get('doctrine_id')} references unknown source")
    for group in groups:
        missing = GROUP_REQUIRED - set(group)
        if missing:
            errors.append(f"module group {group.get('module_group_id')} missing {sorted(missing)}")
        if not set(group.get("member_module_ids", [])).issubset(module_ids):
            errors.append(f"module group {group.get('module_group_id')} references unknown module")
        if _belongs_to_active_type(group, active_types):
            if group.get("approval_status") != "active" or group.get("activation_status") != "active":
                errors.append(f"module group {group.get('module_group_id')} of active type must be active")
        elif group.get("approval_status") != "candidate" or group.get("activation_status") != "inactive":
            errors.append(f"module group {group.get('module_group_id')} must remain candidate/inactive")
    for template in templates:
        missing = TEMPLATE_REQUIRED - set(template)
        if missing:
            errors.append(f"template {template.get('template_id')} missing {sorted(missing)}")
            continue
        if _belongs_to_active_type(template, active_types):
            if template.get("approval_status") != "active" or template.get("activation_status") != "active":
                errors.append(f"template {template.get('template_id')} of active type must be active")
        elif template.get("approval_status") != "candidate" or template.get("activation_status") != "inactive":
            errors.append(f"template {template.get('template_id')} must remain candidate/inactive")
        relative = Path(str(template["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"template {template.get('template_id')} has unsafe path")
            continue
        docx_path = root / relative
        if not docx_path.is_file():
            errors.append(f"template {template.get('template_id')} file missing")
            continue
        try:
            with ZipFile(docx_path) as archive:
                if archive.testzip():
                    errors.append(f"template {template.get('template_id')} has corrupt ZIP member")
                for name in archive.namelist():
                    if name.endswith((".xml", ".rels")):
                        ET.fromstring(archive.read(name))
                document_xml = ET.fromstring(archive.read("word/document.xml"))
                visible_text = "".join(document_xml.itertext())
                semantic_markers = {
                    "type code": str(template.get("type_code", "")),
                    "template name": str(template.get("name", "")),
                    "candidate status": "candidate / inactive",
                    "placeholder marker": "待填充",
                    "lawyer approval gate": "律师批准：未批准",
                    "activation gate": "激活状态：inactive",
                }
                for marker_name, marker in semantic_markers.items():
                    if marker and marker not in visible_text:
                        errors.append(
                            f"template {template.get('template_id')} missing {marker_name}: {marker}"
                        )
                if "律师批准：已批准" in visible_text:
                    errors.append(
                        f"template {template.get('template_id')} falsely claims lawyer approval"
                    )
        except (BadZipFile, ET.ParseError) as exc:
            errors.append(f"template {template.get('template_id')} invalid DOCX/XML: {exc}")
    active_asset_source_ids = {
        source_id
        for item in modules + principles
        if item.get("approval_status") == "active"
        for source_id in item.get("source_ids", [])
    }
    for source in sources:
        missing = SOURCE_REQUIRED - set(source)
        if missing:
            errors.append(f"source {source.get('source_id')} missing {sorted(missing)}")
        if source.get("source_id") in active_asset_source_ids:
            if source.get("approval_status") != "active":
                errors.append(f"source {source.get('source_id')} used by active assets must be active")
        elif source.get("approval_status") != "candidate":
            errors.append(f"source {source.get('source_id')} must remain candidate")
        if not (source.get("url") or source.get("access_scope") or source.get("discovery_channel")):
            errors.append(f"source {source.get('source_id')} lacks locator")
    discovery_source_ids = {
        item.get("source_id") for item in sources
        if item.get("source_kind") == "wechat_practice_article_discovery"
    }
    used_source_ids = {
        source_id
        for item in modules + principles + triggers
        for source_id in item.get("source_ids", [])
    }
    if discovery_source_ids & used_source_ids:
        errors.append("discovery-only WeChat source is used for a legal conclusion")
    for trigger in triggers:
        missing = TRIGGER_REQUIRED - set(trigger)
        if missing:
            errors.append(f"trigger {trigger.get('trigger_id')} missing {sorted(missing)}")
        if _belongs_to_active_type(trigger, active_types):
            if trigger.get("approval_status") != "active" or trigger.get("activation_status") != "active":
                errors.append(f"trigger {trigger.get('trigger_id')} of active type must be active")
        elif trigger.get("approval_status") != "candidate" or trigger.get("activation_status") != "inactive":
            errors.append(f"trigger {trigger.get('trigger_id')} must remain candidate/inactive")
        if not set(trigger.get("required_modules", [])).issubset(module_ids):
            errors.append(f"trigger {trigger.get('trigger_id')} references unknown module")
        if not set(trigger.get("source_ids", [])).issubset(source_ids):
            errors.append(f"trigger {trigger.get('trigger_id')} references unknown source")
    for experience in experiences:
        missing = EXPERIENCE_REQUIRED - set(experience)
        if missing:
            errors.append(f"experience {experience.get('experience_id')} missing {sorted(missing)}")
        if _belongs_to_active_type(experience, active_types):
            if experience.get("approval_status") != "active" or experience.get("activation_status") != "active":
                errors.append(f"experience {experience.get('experience_id')} of active type must be active")
        elif experience.get("approval_status") != "candidate" or experience.get("activation_status") != "inactive":
            errors.append(f"experience {experience.get('experience_id')} must remain candidate/inactive")
        if not set(experience.get("source_ids", [])).issubset(source_ids):
            errors.append(f"experience {experience.get('experience_id')} references unknown source")
    counts = manifest.get("framework_counts", {})
    expected_counts = {
        "principles": len(principles),
        "modules": len(modules),
        "module_groups": len(groups),
        "docx_skeletons": len(templates),
        "triggers": len(triggers),
        "experience_cards": len(experiences),
        "sources": len(sources),
    }
    if counts and counts != expected_counts:
        errors.append(f"manifest framework_counts mismatch: {counts} != {expected_counts}")
    active_modules = {item.get("module_id") for item in modules if item.get("approval_status") == "active"}
    active_principles = {item.get("doctrine_id") for item in principles if item.get("approval_status") == "active"}
    if active_modules != set(manifest.get("active_module_ids", [])):
        errors.append("manifest active_module_ids mismatch")
    if active_principles != set(manifest.get("active_doctrine_ids", [])):
        errors.append("manifest active_doctrine_ids mismatch")

    home_path = re.compile("/" + "Users/")
    family_id = re.compile(r"\bcf-[0-9a-f]{20}\b")
    long_hash = re.compile(r"\b[0-9a-f]{64}\b")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in TEXT_EXTENSIONS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root)
        if home_path.search(text):
            errors.append(f"absolute user path leaked in {relative}")
        if family_id.search(text):
            errors.append(f"contract family id leaked in {relative}")
        if long_hash.search(text):
            errors.append(f"raw hash leaked in {relative}")
    for path in root.rglob("*.docx"):
        try:
            with ZipFile(path) as archive:
                docx_text = "\n".join(
                    archive.read(name).decode("utf-8", errors="replace")
                    for name in archive.namelist()
                    if name.endswith((".xml", ".rels"))
                )
        except BadZipFile:
            continue
        relative = path.relative_to(root)
        if home_path.search(docx_text):
            errors.append(f"absolute user path leaked in {relative}")
        if family_id.search(docx_text):
            errors.append(f"contract family id leaked in {relative}")
        if long_hash.search(docx_text):
            errors.append(f"raw hash leaked in {relative}")
    result = {
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "active_principles": len(active_principles),
        "active_modules": len(active_modules),
        "framework_counts": expected_counts,
        "install_eligible": bool(manifest.get("install_eligible")),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

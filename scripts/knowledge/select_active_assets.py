#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import argparse
import json


SKILL_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = SKILL_ROOT / "assets/knowledge_v2"


def load(name: str) -> dict[str, Any]:
    return json.loads((ASSET_ROOT / name).read_text(encoding="utf-8"))


def applicable(asset: Mapping[str, Any], type_ids: set[str], our_role: str, scene_tags: set[str]) -> bool:
    if asset.get("approval_status") != "active":
        return False
    asset_types = set(asset.get("type_ids", [asset.get("type_id")])) - {None}
    if not asset_types.intersection(type_ids):
        return False
    roles = set(asset.get("roles", []))
    if roles and our_role not in roles and "any" not in roles:
        return False
    required_scenes = set(asset.get("scene_tags", []))
    return not required_scenes or bool(required_scenes.intersection(scene_tags))


def main() -> int:
    parser = argparse.ArgumentParser(description="Select only active principle cards and modules.")
    parser.add_argument("--primary-type-id", required=True)
    parser.add_argument("--secondary-type-id", action="append", default=[])
    parser.add_argument("--our-role", default="unknown")
    parser.add_argument("--scene-tag", action="append", default=[])
    args = parser.parse_args()
    manifest = load("asset_manifest.json")
    type_ids = {args.primary_type_id, *args.secondary_type_id}
    scenes = set(args.scene_tag)
    principles = [
        asset for asset in load("principle_catalog.json")["assets"]
        if applicable(asset, type_ids, args.our_role, scenes)
    ]
    modules = [
        asset for asset in load("module_catalog.json")["assets"]
        if applicable(asset, type_ids, args.our_role, scenes)
    ]
    status = "ok" if principles or modules else "blocked_need_approval"
    print(json.dumps({
        "status": status,
        "primary_type_id": args.primary_type_id,
        "principles": principles,
        "modules": modules,
        "runtime_eligible_status": manifest["runtime_eligible_status"],
        "message": "只返回active资产；空结果时继续原则型审查，不得读取候选正文。",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

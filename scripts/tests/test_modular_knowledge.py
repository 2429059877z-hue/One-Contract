from __future__ import annotations

from pathlib import Path
from subprocess import run
import json
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.knowledge.route_enterprise_contract import load_taxonomy, route


class ModularKnowledgeTests(unittest.TestCase):
    def test_route_uses_stable_interface_and_confirmation_gate(self) -> None:
        result = route(
            title="2026年度框架购销协议",
            legacy_type="procurement_supply",
            our_role="buyer",
            transaction_structure="年度框架购销，按订单采购",
            scene_tags=["annual"],
            taxonomy=load_taxonomy(),
        )
        self.assertEqual(
            set(result),
            {
                "primary_type_id", "secondary_type_ids", "document_kind", "our_role",
                "scene_tags", "classification_status", "evidence", "required_doctrines",
                "required_modules", "human_confirmation_required",
            },
        )
        self.assertEqual(result["classification_status"], "high")
        self.assertFalse(result["human_confirmation_required"])

    def test_unknown_role_never_auto_routes(self) -> None:
        result = route(
            title="房屋租赁合同",
            legacy_type="lease_property",
            our_role="unknown",
            transaction_structure="房屋租赁",
            scene_tags=[],
            taxonomy=load_taxonomy(),
        )
        self.assertTrue(result["human_confirmation_required"])

    def test_candidate_assets_are_not_runtime_selectable(self) -> None:
        completed = run(
            [
                sys.executable,
                "scripts/knowledge/select_active_assets.py",
                "--primary-type-id",
                "type-property-venue-lease",
                "--our-role",
                "lessee",
            ],
            cwd=SKILL_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "blocked_need_approval")
        self.assertEqual(payload["principles"], [])
        self.assertEqual(payload["modules"], [])

    def test_candidate_framework_catalogs_are_complete_but_inactive(self) -> None:
        knowledge = SKILL_ROOT / "assets/knowledge_v2"
        expected = {
            "principle_catalog.json": 20,
            "module_catalog.json": 92,
            "module_group_catalog.json": 41,
            "template_catalog.json": 20,
        }
        active_types = {
            "type-equity-transfer",
            "type-equipment-material-procurement",
            "type-employee-confidentiality",
            "type-noncompete",
            "type-employment-mutual-termination",
        }
        for filename, count in expected.items():
            assets = json.loads((knowledge / filename).read_text(encoding="utf-8"))["assets"]
            self.assertEqual(len(assets), count)
            for item in assets:
                item_types = set(item.get("type_ids") or [item.get("type_id")])
                if item_types & active_types:
                    self.assertEqual(item["approval_status"], "active")
                    self.assertEqual(item["activation_status"], "active")
                else:
                    self.assertEqual(item["approval_status"], "candidate")
                    self.assertEqual(item["activation_status"], "inactive")
        templates = json.loads((knowledge / "template_catalog.json").read_text(encoding="utf-8"))["assets"]
        self.assertTrue(all((SKILL_ROOT / item["relative_path"]).is_file() for item in templates))
        self.assertEqual(len(json.loads((knowledge / "source_catalog.json").read_text(encoding="utf-8"))["assets"]), 51)
        self.assertEqual(len(json.loads((knowledge / "trigger_catalog.json").read_text(encoding="utf-8"))["assets"]), 19)
        self.assertEqual(len(json.loads((knowledge / "experience_catalog.json").read_text(encoding="utf-8"))["assets"]), 27)


if __name__ == "__main__":
    unittest.main()

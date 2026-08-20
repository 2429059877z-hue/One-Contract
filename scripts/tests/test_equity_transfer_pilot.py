from __future__ import annotations

from pathlib import Path
import sys
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.knowledge.detect_equity_transfer_triggers import detect


class EquityTransferPilotTriggerTests(unittest.TestCase):
    def base_facts(self) -> dict:
        return {
            "documents": [
                {
                    "document_id": "final-agreement",
                    "total_consideration": 1000,
                    "superseded_transparently": False,
                    "external_use_only": False,
                }
            ],
            "declared_consideration": 1000,
            "actual_total_consideration": 1000,
            "payments": [],
            "capital_status": {
                "overdue_unpaid": False,
                "noncash_significantly_undervalued": False,
                "withdrawal_evidence": False,
            },
            "preemption": {"external_transfer": False},
        }

    def test_dual_document_price_mismatch_blocks_auto_revision(self) -> None:
        facts = self.base_facts()
        facts["documents"].append(
            {
                "document_id": "external-filing-version",
                "total_consideration": 100,
                "superseded_transparently": False,
                "external_use_only": True,
            }
        )
        facts["declared_consideration"] = 100
        result = detect(facts)
        ids = {item["trigger_id"] for item in result["triggered"]}
        self.assertIn("TRG-EQ-DUAL-DOCUMENT-PRICE", ids)
        self.assertEqual(result["status"], "blocked_auto_revision")
        self.assertFalse(result["automatic_revision_allowed"])
        self.assertTrue(result["prohibited_outputs"])

    def test_transparent_superseding_amendment_does_not_create_false_positive(self) -> None:
        facts = self.base_facts()
        facts["documents"].insert(
            0,
            {
                "document_id": "superseded-original",
                "total_consideration": 900,
                "superseded_transparently": True,
                "external_use_only": False,
            },
        )
        result = detect(facts)
        self.assertEqual(result["triggered"], [])
        self.assertEqual(result["status"], "no_structural_trigger_detected")

    def test_disguised_consideration_requires_no_keyword_inference(self) -> None:
        facts = self.base_facts()
        facts["payments"] = [
            {
                "payment_id": "p-1",
                "label": "往来款",
                "amount": 300,
                "linked_to_equity_transfer": True,
                "independent_commercial_substance": False,
            }
        ]
        result = detect(facts)
        ids = {item["trigger_id"] for item in result["triggered"]}
        self.assertIn("TRG-EQ-DISGUISED-CONSIDERATION", ids)
        self.assertEqual(result["status"], "blocked_auto_revision")

    def test_missing_material_fails_closed_without_accusing_the_parties(self) -> None:
        result = detect({"documents": []})
        self.assertEqual(result["triggered"], [])
        self.assertEqual(result["status"], "human_confirmation_required")
        self.assertTrue(result["missing_evidence"])
        self.assertFalse(result["automatic_revision_allowed"])

    def test_external_transfer_procedure_mismatch_requests_evidence(self) -> None:
        facts = self.base_facts()
        facts["preemption"] = {
            "external_transfer": True,
            "notice_receipt_proved": True,
            "notice_terms_match_final": False,
            "charter_procedure_satisfied": True,
        }
        result = detect(facts)
        self.assertEqual(result["status"], "human_confirmation_required")
        self.assertEqual(result["triggered"][0]["trigger_id"], "TRG-EQ-PREEMPTION-MISMATCH")


if __name__ == "__main__":
    unittest.main()

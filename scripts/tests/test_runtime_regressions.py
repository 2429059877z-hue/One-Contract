from __future__ import annotations

import subprocess
import sys
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


class RuntimeRegressionTests(unittest.TestCase):
    def _write_minimal_unpacked_docx(
        self,
        root: Path,
        *,
        include_comment_parts: bool = False,
    ) -> None:
        files = {
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '<Override PartName="/word/settings.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
                "</Types>"
            ),
            "word/_rels/document.xml.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                "</Relationships>"
            ),
            "word/settings.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "</w:settings>"
            ),
            "word/document.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body>"
                "<w:p><w:r><w:t>第一条 合同目的</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>第二条 付款安排</w:t></w:r></w:p>"
                "</w:body>"
                "</w:document>"
            ),
        }
        if include_comment_parts:
            files.update(
                {
                    "word/comments.xml": (
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
                        "</w:comments>"
                    ),
                    "word/commentsExtended.xml": (
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml">'
                        "</w15:commentsEx>"
                    ),
                    "word/commentsIds.xml": (
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<w16cid:commentsIds xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid">'
                        "</w16cid:commentsIds>"
                    ),
                    "word/commentsExtensible.xml": (
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<w16cex:commentsExtensible xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex">'
                        "</w16cex:commentsExtensible>"
                    ),
                }
            )

        for relative_path, content in files.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _pack(self, unpacked: Path, output: Path) -> None:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(item for item in unpacked.rglob("*") if item.is_file()):
                archive.write(path, path.relative_to(unpacked).as_posix())

    def test_docx_xml_editor_injects_timestamp_attributes(self) -> None:
        from scripts.docx_engine.document import DocxXMLEditor

        fixed_timestamp = datetime(2026, 6, 4, 9, 30, tzinfo=timezone.utc)
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>party</w:t></w:r></w:p></w:body>"
            "</w:document>"
        )

        with TemporaryDirectory() as temp_dir:
            xml_path = Path(temp_dir) / "document.xml"
            xml_path.write_text(xml, encoding="utf-8")
            editor = DocxXMLEditor(
                xml_path,
                rsid="12345678",
                author="Reviewer",
                initials="RV",
                timestamp_provider=lambda: fixed_timestamp,
            )

            paragraph = editor.get_node(tag="w:p")
            editor.append_to(paragraph, '<w:ins><w:r><w:t> addition</w:t></w:r></w:ins>')

            inserted = editor.get_node(tag="w:ins")
            expected_timestamp = fixed_timestamp.astimezone().isoformat(
                timespec="seconds"
            )
            self.assertEqual(inserted.getAttribute("w:author"), "Reviewer")
            self.assertEqual(inserted.getAttribute("w:date"), expected_timestamp)
            self.assertEqual(
                inserted.getAttribute("w16du:dateUtc"),
                fixed_timestamp.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )

    def test_add_comment_creates_missing_comment_extensible_part(self) -> None:
        from scripts.docx_engine.reviewer import ContractReviewer

        fixed_timestamp = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temp_dir:
            unpacked = Path(temp_dir) / "unpacked"
            self._write_minimal_unpacked_docx(unpacked)

            reviewer = ContractReviewer(unpacked, author="Reviewer", initials="")
            reviewer.set_operation_timestamp(lambda: fixed_timestamp)
            reviewer.add_comment(reviewer.find_text("第一条"), "补充合同目的")

            extensible = reviewer.doc["word/commentsExtensible.xml"]
            comment_ext = extensible.get_node(tag="w16cex:commentExtensible")
            self.assertEqual(
                comment_ext.getAttribute("w16cex:dateUtc"),
                "2026-06-13T10:00:00Z",
            )

    def test_add_comment_consumes_one_timestamp_per_comment(self) -> None:
        from scripts.docx_engine.reviewer import ContractReviewer

        fixed_timestamp = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)
        calls = []

        def provider():
            value = fixed_timestamp + timedelta(minutes=len(calls))
            calls.append(value)
            return value

        with TemporaryDirectory() as temp_dir:
            unpacked = Path(temp_dir) / "unpacked"
            self._write_minimal_unpacked_docx(unpacked, include_comment_parts=True)

            reviewer = ContractReviewer(unpacked, author="Reviewer", initials="")
            reviewer.set_operation_timestamp(provider)
            reviewer.add_comment(reviewer.find_text("第一条"), "补充合同目的")
            reviewer.add_comment(reviewer.find_text("第二条"), "补充付款安排")

            comments = reviewer.doc["word/comments.xml"].dom.getElementsByTagName(
                "w:comment"
            )
            extensible_comments = reviewer.doc[
                "word/commentsExtensible.xml"
            ].dom.getElementsByTagName("w16cex:commentExtensible")

            self.assertEqual(len(calls), 2)
            self.assertEqual(
                [item.getAttribute("w:date") for item in comments],
                [
                    fixed_timestamp.astimezone().isoformat(timespec="seconds"),
                    (fixed_timestamp + timedelta(minutes=1))
                    .astimezone()
                    .isoformat(timespec="seconds"),
                ],
            )
            self.assertEqual(
                [
                    item.getAttribute("w16cex:dateUtc")
                    for item in extensible_comments
                ],
                [
                    "2026-06-13T10:00:00Z",
                    "2026-06-13T10:01:00Z",
                ],
            )

    def test_review_timeline_uses_real_execution_time_without_future_simulation(self) -> None:
        from scripts.review import review_runtime

        class MinRandom:
            def randint(self, start, end):
                return start

        fixed_now = datetime(2026, 6, 13, 10, 0, 0, 123456, tzinfo=timezone.utc)
        original_get_local_now = review_runtime.get_local_now
        original_get_local_timezone = review_runtime.get_local_timezone
        try:
            review_runtime.get_local_now = lambda: fixed_now
            review_runtime.get_local_timezone = lambda: timezone.utc

            timeline = review_runtime.ReviewTimeline(rng=MinRandom())
            first_provider = timeline.start_finding()
            first = first_provider()
            timeline.complete_finding()

            second_provider = timeline.start_finding()
            second = second_provider()

            expected = fixed_now.replace(microsecond=0)
            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
        finally:
            review_runtime.get_local_now = original_get_local_now
            review_runtime.get_local_timezone = original_get_local_timezone

    def test_apply_review_plan_can_be_run_directly(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/review/apply_review_plan.py",
                "--help",
            ],
            cwd=SKILL_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--input", result.stdout)
        self.assertIn("--plan", result.stdout)

    def test_reporting_can_be_run_directly(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/report/reporting.py", "--help"],
            cwd=SKILL_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--execution", result.stdout)

    def test_default_runtime_paths_point_to_skill_root(self) -> None:
        from scripts.review import archive_service, review_runtime

        self.assertEqual(review_runtime.SKILL_ROOT, SKILL_ROOT)
        self.assertEqual(review_runtime.DEFAULT_CONFIG_DIR, SKILL_ROOT / "config")
        self.assertEqual(
            review_runtime.PROFILE_TEMPLATE_PATH,
            SKILL_ROOT / "config" / "reviewer_profile.example.json",
        )
        self.assertEqual(archive_service.SKILL_ROOT, SKILL_ROOT)
        self.assertEqual(archive_service.DEFAULT_ARCHIVE_DIR, SKILL_ROOT / "archive")

    def test_xml_editor_always_saves_utf8(self) -> None:
        from scripts.docx_engine.utilities import XMLEditor

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "part.xml"
            path.write_bytes(
                b'<?xml version="1.0" encoding="ascii"?><root>&#21512;&#21516;</root>'
            )
            XMLEditor(path).save()
            self.assertIn(b'encoding="utf-8"', path.read_bytes()[:100].lower())

    def test_normalized_cross_run_locator_creates_real_revisions(self) -> None:
        from scripts.docx_engine.reviewer import ContractReviewer

        with TemporaryDirectory() as temp_dir:
            unpacked = Path(temp_dir) / "unpacked"
            self._write_minimal_unpacked_docx(unpacked)
            document = unpacked / "word/document.xml"
            document.write_text(
                document.read_text(encoding="utf-8").replace(
                    "<w:r><w:t>第一条 合同目的</w:t></w:r>",
                    "<w:r><w:t>第一条 协商</w:t></w:r><w:r><w:t>\u00a0解除</w:t></w:r>",
                ),
                encoding="utf-8",
            )
            reviewer = ContractReviewer(unpacked, author="Reviewer", initials="RV")
            result = reviewer.replace_text("协商 解除", "协商一致解除", tag="w:r")
            dom = reviewer.doc["word/document.xml"].dom
            self.assertEqual(result["fallback"], "paragraph_fragment")
            self.assertGreater(len(dom.getElementsByTagName("w:del")), 0)
            self.assertGreater(len(dom.getElementsByTagName("w:ins")), 0)
            self.assertIsNotNone(reviewer.find_text("协商一致解除", tag="w:p"))

    def test_existing_revision_requires_explicit_baseline_strategy(self) -> None:
        from scripts.docx_engine.reviewer import ContractReviewer

        with TemporaryDirectory() as temp_dir:
            unpacked = Path(temp_dir) / "unpacked"
            self._write_minimal_unpacked_docx(unpacked)
            document = unpacked / "word/document.xml"
            document.write_text(
                document.read_text(encoding="utf-8").replace(
                    "<w:r><w:t>第一条 合同目的</w:t></w:r>",
                    '<w:ins w:id="7" w:author="Old" w:date="2026-01-01T00:00:00Z">'
                    "<w:r><w:t>第一条 合同目的</w:t></w:r></w:ins>",
                ),
                encoding="utf-8",
            )
            reviewer = ContractReviewer(unpacked, author="Reviewer", initials="RV")
            with self.assertRaisesRegex(ValueError, "accept-existing"):
                reviewer.replace_text("合同目的", "协议目的", tag="w:r")

    def test_evidence_state_drives_action_without_keyword_rules(self) -> None:
        from scripts.review.action_executor import (
            infer_auto_action,
            resolve_delivery_action,
            resolve_revision_comment,
        )

        assumption = {
            "action": "auto",
            "evidence_state": "reasonable_assumption",
            "target_text": "付款安排",
            "replacement_text": "在验收后十日内付款",
            "unknown_facts": ["验收日期待确认"],
            "risk": "付款触发点缺少证据",
        }
        action = infer_auto_action(assumption)
        self.assertEqual(action, "replace")
        self.assertIsNotNone(
            resolve_revision_comment(assumption, action=action, requested_action="auto")
        )
        major = {**assumption, "evidence_state": "major_choice"}
        self.assertEqual(infer_auto_action(major), "comment")
        self.assertEqual(
            resolve_delivery_action(
                major,
                requested_action="replace",
                action="replace",
                edit_policy="revise-first",
            ),
            "comment",
        )

    def test_quality_gate_builds_accepted_and_rejected_views(self) -> None:
        from scripts.docx_engine.pack import pack_document
        from scripts.docx_engine.quality_gate import run_quality_gate
        from scripts.docx_engine.reviewer import ContractReviewer

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unpacked = root / "unpacked"
            self._write_minimal_unpacked_docx(unpacked)
            original = root / "original.docx"
            self._pack(unpacked, original)

            reviewer = ContractReviewer(unpacked, author="Reviewer", initials="RV")
            reviewer.replace_text(
                "第二条 付款安排",
                "第二条 验收后十日内付款",
                tag="w:r",
                comment_text="付款触发点与期限一并明确。",
            )
            reviewer.save()
            reviewed = root / "reviewed.docx"
            self.assertTrue(pack_document(unpacked, reviewed, validate=True))

            report = run_quality_gate(
                reviewed,
                original_docx=original,
                output_dir=root / "quality",
                baseline_view="reject",
                require_revisions=True,
            )
            self.assertEqual(report["status"], "PASS", report["errors"])
            self.assertTrue(Path(report["accepted_docx"]).exists())
            self.assertTrue(Path(report["rejected_docx"]).exists())
            self.assertTrue(report["semantic_baseline_equal"])

    def test_quality_gate_rejects_ascii_xml_declaration(self) -> None:
        from scripts.docx_engine.quality_gate import inspect_docx

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unpacked = root / "unpacked"
            self._write_minimal_unpacked_docx(unpacked)
            document = unpacked / "word/document.xml"
            document.write_text(
                document.read_text(encoding="utf-8").replace("UTF-8", "ascii"),
                encoding="ascii",
                errors="xmlcharrefreplace",
            )
            candidate = root / "ascii.docx"
            self._pack(unpacked, candidate)
            report = inspect_docx(candidate)
            self.assertEqual(report["status"], "FAIL")
            self.assertTrue(any("ASCII" in item.upper() for item in report["errors"]))

    def test_accepting_revision_prunes_comment_whose_anchor_disappears(self) -> None:
        from defusedxml import minidom
        from scripts.docx_engine.revision_views import resolve_unpacked_revisions

        with TemporaryDirectory() as temp_dir:
            unpacked = Path(temp_dir) / "unpacked"
            self._write_minimal_unpacked_docx(unpacked, include_comment_parts=True)
            document = unpacked / "word/document.xml"
            document.write_text(
                document.read_text(encoding="utf-8").replace(
                    "<w:r><w:t>第一条 合同目的</w:t></w:r>",
                    '<w:del w:id="1" w:author="Old" w:date="2026-01-01T00:00:00Z">'
                    '<w:commentRangeStart w:id="0"/>'
                    "<w:r><w:delText>第一条 合同目的</w:delText></w:r>"
                    '<w:commentRangeEnd w:id="0"/>'
                    '<w:r><w:commentReference w:id="0"/></w:r>'
                    "</w:del>",
                ),
                encoding="utf-8",
            )
            comments = unpacked / "word/comments.xml"
            comments.write_text(
                comments.read_text(encoding="utf-8").replace(
                    "</w:comments>",
                    '<w:comment w:id="0"><w:p w14:paraId="AAAA0001">'
                    "<w:r><w:t>旧批注</w:t></w:r></w:p></w:comment></w:comments>",
                ),
                encoding="utf-8",
            )

            report = resolve_unpacked_revisions(unpacked, "accept")
            comments_dom = minidom.parse(str(comments))
            document_dom = minidom.parse(str(document))
            self.assertEqual(report["comment_resolution"]["orphan_comment_ids"], ["0"])
            self.assertEqual(len(comments_dom.getElementsByTagName("w:comment")), 0)
            self.assertEqual(
                len(document_dom.getElementsByTagName("w:commentReference")), 0
            )

    def test_paragraph_replacement_preserves_existing_comment_anchors(self) -> None:
        from scripts.docx_engine.pack import pack_document
        from scripts.docx_engine.quality_gate import inspect_docx
        from scripts.docx_engine.reviewer import ContractReviewer

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unpacked = root / "unpacked"
            self._write_minimal_unpacked_docx(unpacked)
            reviewer = ContractReviewer(unpacked, author="Reviewer", initials="RV")
            reviewer.add_comment(reviewer.find_text("第一条"), "既有批注")
            reviewer.replace_text(
                "第一条 合同目的", "第一条 协议目的", tag="w:p"
            )
            reviewer.save()
            reviewed = root / "reviewed.docx"
            self.assertTrue(pack_document(unpacked, reviewed, validate=True))
            report = inspect_docx(reviewed)
            self.assertEqual(report["status"], "PASS", report["errors"])
            self.assertEqual(report["metrics"]["comment_count"], 1)

    def test_review_report_discloses_execution_and_quality_failures(self) -> None:
        from scripts.report.reporting import render_review_report

        report = render_review_report(
            plan={
                "meta": {"contract_name": "测试合同"},
                "findings": [{"id": "F-1", "risk": "测试风险"}],
            },
            execution={
                "applied": 0,
                "failed": 1,
                "skipped": 0,
                "report_only": 0,
                "results": [
                    {"id": "F-1", "status": "failed", "message": "目标未命中"}
                ],
                "quality_gate": {"status": "FAIL", "errors": ["批注锚点断裂"]},
            },
        )
        self.assertIn("F-1：目标未命中", report)
        self.assertIn("批注锚点断裂", report)
        self.assertIn("不得作为正式交付版本", report)

    def test_review_report_consumes_principle_plan_fields(self) -> None:
        from scripts.report.reporting import render_review_report

        report = render_review_report(
            plan={
                "meta": {
                    "contract_name": "协商解除协议",
                    "party_role": "甲方",
                    "transaction_profile": "处理解除、结算与交接。",
                },
                "findings": [
                    {
                        "id": "ET-1",
                        "risk_level": "P1",
                        "title": "争议处理程序",
                        "description": "原约定与法定程序不一致。",
                        "rationale": "按法定程序重写，不需要商业授权。",
                        "replacement_text": "依法向有管辖权的机构申请处理。",
                        "basis_type": "legal_boundary",
                        "basis": "现行有效的劳动争议处理规则",
                    }
                ],
            },
            execution={
                "applied": 1,
                "failed": 0,
                "skipped": 0,
                "report_only": 0,
                "results": [{"id": "ET-1", "status": "applied"}],
                "quality_gate": {"status": "PASS", "errors": []},
            },
        )
        self.assertIn("合同类型：协商解除协议", report)
        self.assertIn("审查意见：按法定程序重写", report)
        self.assertIn("法律依据：现行有效的劳动争议处理规则", report)
        # 对外意见书正常成功时不披露 DOCX/质量门等技术状态
        self.assertNotIn("DOCX 落痕与质量状态", report)
        self.assertNotIn("报告完整性", report)
        self.assertNotIn("所属部门：未设置", report)


if __name__ == "__main__":
    unittest.main()

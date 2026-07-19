#!/usr/bin/env python3
"""Unit tests for deterministic GitHub CSV to ServiceNow DNS orchestration."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tests.load_handler import load_handler

handler = load_handler("servicenow-csv-dns")


CSV_TEXT = """Subnet,IPv4,IPv6,DNS,A,AAAA
block,10.0.0.0/24,2001:db8::/64,,,
link,10.0.0.0/30,2001:db8::/127,SW2,10.0.0.2,2001:db8::2
,,,sw1,10.0.0.1,2001:db8::1
,,,host-no-v6,10.0.0.3,N/A
"""


class CsvPlanTests(unittest.TestCase):
    def test_a_plan_is_normalized_sorted_and_tracks_skips(self) -> None:
        plan, errors, skipped, total = handler._build_plan(CSV_TEXT, "Prajwal.Dev.", "A")
        self.assertEqual(total, 4)
        self.assertEqual(errors, [])
        self.assertEqual([item["fqdn"] for item in plan], [
            "host-no-v6.prajwal.dev", "sw1.prajwal.dev", "sw2.prajwal.dev"
        ])
        self.assertEqual(plan[1]["ip"], "10.0.0.1")
        self.assertEqual(len(skipped), 1)

    def test_aaaa_plan_skips_na(self) -> None:
        plan, errors, skipped, _ = handler._build_plan(CSV_TEXT, "prajwal.dev", "AAAA")
        self.assertEqual(errors, [])
        self.assertEqual(len(plan), 2)
        self.assertEqual(len(skipped), 2)
        self.assertEqual(plan[0]["record_type"], "AAAA")

    def test_both_blocks_dual_stack_rows(self) -> None:
        plan, errors, _, _ = handler._build_plan(CSV_TEXT, "prajwal.dev", "both")
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(errors), 2)
        self.assertIn("cannot both be modeled", errors[0]["error"])

    def test_missing_required_header_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required columns"):
            handler._build_plan("DNS,A\nsw1,10.0.0.1\n", "prajwal.dev")

    def test_duplicate_fqdn_blocks_plan(self) -> None:
        content = "DNS,A,AAAA\nsw1,10.0.0.1,\nSW1,10.0.0.2,\n"
        plan, errors, _, _ = handler._build_plan(content, "prajwal.dev")
        self.assertEqual(len(plan), 1)
        self.assertIn("Duplicate generated FQDN", errors[0]["error"])

    def test_duplicate_ip_blocks_plan(self) -> None:
        content = "DNS,A,AAAA\nsw1,10.0.0.1,\nsw2,10.0.0.1,\n"
        _, errors, _, _ = handler._build_plan(content, "prajwal.dev")
        self.assertIn("Duplicate selected IP", errors[0]["error"])

    def test_invalid_selected_address_is_reported(self) -> None:
        content = "DNS,A,AAAA\nsw1,999.1.1.1,\n"
        _, errors, _, _ = handler._build_plan(content, "prajwal.dev")
        self.assertIn("Invalid IPv4", errors[0]["error"])


class WorkflowTests(unittest.TestCase):
    PAYLOAD = {
        "content": "DNS,A,AAAA\nsw1,10.0.0.1,\nsw2,10.0.0.2,\n",
        "path": "output/plan.csv",
        "sha": "abc123",
        "html_url": "https://github.com/example-owner/repo/blob/main/output/plan.csv",
        "truncated": False,
    }

    @patch.object(handler, "_github_file")
    def test_preview_is_non_mutating_and_auditable(self, github_mock) -> None:
        github_mock.return_value = dict(self.PAYLOAD)
        result = handler.preview_github_csv_dns(
            repo="repo", path="output/plan.csv", domain="prajwal.dev", record_type="A"
        )
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])
        self.assertEqual(result["results"]["source"]["revision"], "abc123")
        self.assertEqual(result["results"]["planned_count"], 2)

    @patch.object(handler, "_load_module")
    @patch.object(handler, "_github_file")
    def test_confirmed_import_reports_created_and_unchanged(self, github_mock, load_mock) -> None:
        github_mock.return_value = dict(self.PAYLOAD)
        dns = Mock()
        dns.handle_command.side_effect = [
            {"status": "success", "results": {"created": True}, "next_steps": []},
            {"status": "success", "results": {"created": False}, "next_steps": []},
        ]
        load_mock.return_value = dns
        result = handler.import_github_csv_dns(
            repo="repo", path="output/plan.csv", domain="prajwal.dev", confirm=True
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"]["created_count"], 1)
        self.assertEqual(result["results"]["unchanged_count"], 1)
        self.assertEqual(dns.handle_command.call_count, 2)

    @patch.object(handler, "_load_module")
    @patch.object(handler, "_github_file")
    def test_validation_error_blocks_all_mutation(self, github_mock, load_mock) -> None:
        payload = dict(self.PAYLOAD)
        payload["content"] = "DNS,A,AAAA\nsw1,bad,\n"
        github_mock.return_value = payload
        result = handler.import_github_csv_dns(
            repo="repo", path="output/plan.csv", domain="prajwal.dev", confirm=True
        )
        self.assertEqual(result["status"], "error")
        load_mock.assert_not_called()

    @patch.object(handler, "_load_module")
    @patch.object(handler, "_github_file")
    def test_verify_counts_complete_mappings(self, github_mock, load_mock) -> None:
        github_mock.return_value = dict(self.PAYLOAD)
        dns = Mock()
        dns.handle_command.side_effect = [
            {"status": "success", "results": {"exists": True}, "next_steps": []},
            {"status": "warning", "results": {"exists": False}, "next_steps": []},
        ]
        load_mock.return_value = dns
        result = handler.verify_github_csv_dns(
            repo="repo", path="output/plan.csv", domain="prajwal.dev"
        )
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["results"]["verified_count"], 1)
        self.assertEqual(result["results"]["missing_count"], 1)

    @patch.object(handler, "_load_module")
    @patch.object(handler, "_github_file")
    def test_delete_preview_is_non_mutating(self, github_mock, load_mock) -> None:
        github_mock.return_value = dict(self.PAYLOAD)
        result = handler.delete_github_csv_dns(repo="repo", path="output/plan.csv", domain="prajwal.dev")
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["deleted"])
        load_mock.assert_not_called()

    @patch.object(handler, "_load_module")
    @patch.object(handler, "_github_file")
    def test_confirmed_delete_reports_counts(self, github_mock, load_mock) -> None:
        github_mock.return_value = dict(self.PAYLOAD)
        dns = Mock()
        dns.handle_command.side_effect = [
            {"status": "success", "results": {"deleted": True}, "next_steps": []},
            {"status": "success", "results": {"deleted": False}, "next_steps": []},
        ]
        load_mock.return_value = dns
        result = handler.delete_github_csv_dns(repo="repo", path="output/plan.csv", domain="prajwal.dev", confirm=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"]["deleted_count"], 1)
        self.assertEqual(result["results"]["unchanged_count"], 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Unit tests for the constrained ServiceNow DNS skill."""

from __future__ import annotations

import unittest
from unittest.mock import call, patch

from tests.load_handler import load_handler

handler = load_handler("servicenow-dns")


class ValidationTests(unittest.TestCase):
    def test_normalizes_fqdn_and_ip(self) -> None:
        self.assertEqual(handler._fqdn("SW1", "Prajwal.Dev."), "sw1.prajwal.dev")
        self.assertEqual(handler._validate_ip("2001:0db8::1"), "2001:db8::1")

    def test_rejects_fqdn_as_host_label(self) -> None:
        with self.assertRaisesRegex(ValueError, "single host label"):
            handler._validate_hostname("sw1.example.com")

    def test_rejects_invalid_domain_and_ip(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid DNS domain"):
            handler._validate_domain("localhost")
        with self.assertRaisesRegex(ValueError, "valid IPv4 or IPv6"):
            handler._validate_ip("999.1.1.1")

    def test_request_restricts_method_and_table(self) -> None:
        with self.assertRaisesRegex(ValueError, "DELETE requires"):
            handler._request("DELETE", f"/api/now/table/{handler.DNS_TABLE}")
        with self.assertRaisesRegex(ValueError, "only ServiceNow DNS/IP"):
            handler._request("POST", "/api/now/table/incident")


class DnsCommandTests(unittest.TestCase):
    @patch.object(handler, "_find_records", return_value=[])
    def test_create_requires_confirmation(self, find_mock) -> None:
        result = handler.create_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175", confirm=False
        )
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])
        find_mock.assert_not_called()

    @patch.object(handler, "_find_relationships")
    @patch.object(handler, "_find_ip_records")
    @patch.object(handler, "_find_records")
    def test_create_is_idempotent(self, find_mock, find_ip_mock, find_relationship_mock) -> None:
        find_mock.return_value = [
            {"sys_id": "a" * 32, "fqdn": "sw1.prajwal.dev", "ip_address": "10.10.20.175"}
        ]
        find_ip_mock.return_value = [{"sys_id": "b" * 32, "ip_address": "10.10.20.175"}]
        find_relationship_mock.return_value = [{"sys_id": "c" * 32}]
        result = handler.create_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175", confirm=True
        )
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["results"]["created"])

    @patch.object(handler, "_find_records")
    def test_create_stops_on_conflict(self, find_mock) -> None:
        find_mock.return_value = [
            {"sys_id": "a" * 32, "fqdn": "sw1.prajwal.dev", "ip_address": "10.10.20.99"}
        ]
        result = handler.create_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175", confirm=True
        )
        self.assertEqual(result["status"], "error")

    @patch.object(handler, "_create_relationship")
    @patch.object(handler, "_find_relationships", return_value=[])
    @patch.object(handler, "_create_ip_record")
    @patch.object(handler, "_find_ip_records", return_value=[])
    @patch.object(handler, "_request")
    @patch.object(handler, "_find_records", return_value=[])
    def test_create_posts_minimal_dns_ci(
        self, find_mock, request_mock, find_ip_mock, create_ip_mock,
        find_relationship_mock, create_relationship_mock,
    ) -> None:
        request_mock.return_value = {
            "result": {
                "sys_id": "a" * 32,
                "fqdn": "sw1.prajwal.dev",
                "ip_address": "10.10.20.175",
            }
        }
        create_ip_mock.return_value = {"sys_id": "b" * 32, "ip_address": "10.10.20.175"}
        create_relationship_mock.return_value = {"sys_id": "c" * 32}
        result = handler.create_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175", confirm=True
        )
        self.assertTrue(result["results"]["created"])
        method, path = request_mock.call_args.args
        payload = request_mock.call_args.kwargs["payload"]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/api/now/table/{handler.DNS_TABLE}")
        self.assertEqual(payload["name"], "sw1.prajwal.dev")
        self.assertEqual(payload["fqdn"], "sw1.prajwal.dev")
        self.assertEqual(payload["ip_address"], "10.10.20.175")

    @patch.object(handler, "_find_relationships", return_value=[{"sys_id": "c" * 32}])
    @patch.object(handler, "_find_ip_records", return_value=[{"sys_id": "b" * 32}])
    @patch.object(handler, "_find_records")
    def test_verify_requires_exact_mapping(
        self, find_mock, find_ip_mock, find_relationship_mock
    ) -> None:
        find_mock.return_value = [
            {"sys_id": "a" * 32, "fqdn": "sw1.prajwal.dev", "ip_address": "10.10.20.175"}
        ]
        result = handler.verify_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175"
        )
        self.assertTrue(result["results"]["exists"])


class DeterministicDnsImportTests(unittest.TestCase):
    INVENTORY = [
        {"hostname": "sw2.example.net", "managementIpAddress": "10.10.20.176"},
        {"hostname": "sw1", "managementIpAddress": "10.10.20.175"},
    ]

    def test_normalizes_current_catalyst_inventory_response(self) -> None:
        result = handler._normalize_catalyst_inventory(
            {
                "status": "success",
                "results": [
                    {"hostname": "sw1", "managementIpAddress": "10.10.20.175", "id": "one"}
                ],
                "next_steps": [],
            }
        )
        self.assertEqual(result[0]["managementIpAddress"], "10.10.20.175")
        self.assertEqual(result[0]["id"], "one")

    def test_normalizes_legacy_catalyst_inventory_response(self) -> None:
        result = handler._normalize_catalyst_inventory(
            {
                "status": "success",
                "results": {
                    "devices": [
                        {"hostname": "sw1", "management_ip": "10.10.20.175", "device_id": "one"}
                    ]
                },
                "next_steps": [],
            }
        )
        self.assertEqual(result[0]["managementIpAddress"], "10.10.20.175")
        self.assertEqual(result[0]["id"], "one")

    @patch.object(handler, "_preflight_dns_plan")
    @patch.object(handler, "_catalyst_inventory")
    def test_preview_is_sorted_and_non_mutating(self, inventory_mock, preflight_mock) -> None:
        inventory_mock.return_value = list(self.INVENTORY)
        preflight_mock.side_effect = lambda plan: ([{**item, "action": "create"} for item in plan], [])
        result = handler.import_catalyst_dns(domain="prajwal.dev", confirm=False)
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])
        self.assertEqual(
            [item["fqdn"] for item in result["results"]["plan"]],
            ["sw1.prajwal.dev", "sw2.prajwal.dev"],
        )

    @patch.object(handler, "_preflight_dns_plan")
    @patch.object(handler, "_catalyst_inventory")
    def test_unchanged_preview_does_not_recommend_import(
        self, inventory_mock, preflight_mock
    ) -> None:
        inventory_mock.return_value = list(self.INVENTORY)
        preflight_mock.side_effect = lambda plan: (
            [{**item, "action": "unchanged"} for item in plan],
            [],
        )
        result = handler.import_catalyst_dns(domain="prajwal.dev", confirm=False)
        self.assertIn("no confirmed import", result["next_steps"][0].lower())

    @patch.object(handler, "_preflight_dns_plan")
    @patch.object(handler, "create_dns")
    @patch.object(handler, "_catalyst_inventory")
    def test_confirmed_import_reports_unchanged_records(
        self, inventory_mock, create_mock, preflight_mock
    ) -> None:
        inventory_mock.return_value = list(self.INVENTORY)
        preflight_mock.side_effect = lambda plan: ([{**item, "action": "unchanged"} for item in plan], [])
        create_mock.side_effect = [
            {"status": "success", "results": {"created": False}, "next_steps": []},
            {"status": "success", "results": {"created": False}, "next_steps": []},
        ]
        result = handler.import_catalyst_dns(domain="prajwal.dev", confirm=True)
        self.assertEqual(result["results"]["created_count"], 0)
        self.assertEqual(result["results"]["unchanged_count"], 2)

    @patch.object(handler, "create_dns")
    @patch.object(handler, "_catalyst_inventory")
    def test_duplicate_ip_blocks_all_mutation(self, inventory_mock, create_mock) -> None:
        inventory_mock.return_value = [
            {"hostname": "sw1", "managementIpAddress": "10.10.20.175"},
            {"hostname": "sw2", "managementIpAddress": "10.10.20.175"},
        ]
        result = handler.import_catalyst_dns(domain="prajwal.dev", confirm=True)
        self.assertEqual(result["status"], "error")
        create_mock.assert_not_called()

    @patch.object(handler, "_find_relationships")
    @patch.object(handler, "_find_ip_records")
    @patch.object(handler, "_find_records")
    def test_preflight_blocks_conflicts_before_mutation(
        self, find_mock, find_ip_mock, find_relationship_mock
    ) -> None:
        find_mock.side_effect = [
            [{"sys_id": "a" * 32, "fqdn": "sw1.prajwal.dev", "ip_address": "10.10.20.99"}],
            [],
        ]
        find_ip_mock.return_value = []
        find_relationship_mock.return_value = []
        checked, conflicts = handler._preflight_dns_plan(
            [
                {"hostname": "sw1", "domain": "prajwal.dev", "fqdn": "sw1.prajwal.dev", "ip": "10.10.20.175"},
                {"hostname": "sw2", "domain": "prajwal.dev", "fqdn": "sw2.prajwal.dev", "ip": "10.10.20.176"},
            ]
        )
        self.assertEqual(checked[0]["action"], "blocked")
        self.assertEqual(checked[1]["action"], "create")
        self.assertEqual(len(conflicts), 1)

    def test_delete_requires_confirmation(self) -> None:
        result = handler.delete_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175", confirm=False
        )
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["deleted"])

    @patch.object(handler, "_request", return_value={"result": []})
    @patch.object(handler, "_delete_record")
    @patch.object(handler, "_find_relationships", return_value=[{"sys_id": "c" * 32}])
    @patch.object(handler, "_find_ip_records", return_value=[{"sys_id": "b" * 32}])
    @patch.object(handler, "_find_records")
    def test_delete_removes_relationship_dns_and_orphan_ip(
        self, find_mock, find_ip_mock, find_relationship_mock, delete_mock, request_mock
    ) -> None:
        find_mock.return_value = [
            {
                "sys_id": "a" * 32,
                "fqdn": "sw1.prajwal.dev",
                "ip_address": "10.10.20.175",
            }
        ]
        result = handler.delete_dns(
            hostname="sw1", domain="prajwal.dev", ip="10.10.20.175", confirm=True
        )
        self.assertTrue(result["results"]["deleted"])
        self.assertTrue(result["results"]["ip_deleted"])
        self.assertEqual(
            delete_mock.call_args_list,
            [
                call(handler.RELATION_TABLE, "c" * 32),
                call(handler.DNS_TABLE, "a" * 32),
                call(handler.IP_TABLE, "b" * 32),
            ],
        )


if __name__ == "__main__":
    unittest.main()

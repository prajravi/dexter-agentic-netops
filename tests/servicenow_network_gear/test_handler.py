#!/usr/bin/env python3
"""Tests for guarded ServiceNow Network Gear CRUD."""

from __future__ import annotations

import unittest
from unittest.mock import call, patch

from tests.load_handler import load_handler

handler = load_handler("servicenow-network-gear")


class ValidationTests(unittest.TestCase):
    def test_normalizes_inventory_values(self) -> None:
        self.assertEqual(handler._validate_hostname("SW1"), "sw1")
        self.assertEqual(handler._validate_ip("2001:0db8::1"), "2001:db8::1")
        self.assertEqual(handler._validate_mac("52-54-00-02-19-54"), "52:54:00:02:19:54")

    def test_rejects_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "hostname"):
            handler._validate_hostname("bad host")
        with self.assertRaisesRegex(ValueError, "serial"):
            handler._validate_serial("bad serial")
        with self.assertRaisesRegex(ValueError, "IPv4 or IPv6"):
            handler._validate_ip("999.1.1.1")

    def test_record_path_restricts_tables_and_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "restricted"):
            handler._record_path("incident")
        with self.assertRaisesRegex(ValueError, "32-character"):
            handler._record_path(handler.CI_TABLE, "bad")


class CrudTests(unittest.TestCase):
    def test_create_requires_confirmation(self) -> None:
        result = handler.create_gear(
            hostname="sw1", ip="10.10.20.175", serial="CML12345UAD",
            platform="C9KV-UADP-8P", software="17.12.1prd9",
            catalyst_id="0a84b333-fedc-4097-9100-ec806f8e1d11", confirm=False,
        )
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])

    @patch.object(handler, "_find_assets", return_value=[{"sys_id": "b" * 32}])
    @patch.object(handler, "_find_ci")
    def test_create_is_idempotent(self, find_mock, assets_mock) -> None:
        find_mock.return_value = [{
            "sys_id": "a" * 32,
            "serial_number": "CML12345UAD",
            "correlation_id": f"{handler.MANAGED_PREFIX}device-1",
        }]
        result = handler.create_gear(
            hostname="sw1", ip="10.10.20.175", serial="CML12345UAD",
            platform="C9KV-UADP-8P", software="17.12.1prd9",
            catalyst_id="device-1", confirm=True,
        )
        self.assertFalse(result["results"]["created"])

    @patch.object(handler, "_request")
    @patch.object(handler, "_reference_sys_id")
    @patch.object(handler, "_find_ci", return_value=[])
    def test_create_posts_ci_then_asset(self, find_mock, reference_mock, request_mock) -> None:
        reference_mock.side_effect = ["b" * 32, "c" * 32]
        request_mock.side_effect = [
            {"result": {"sys_id": "a" * 32, "serial_number": "CML12345UAD"}},
            {"result": {"sys_id": "d" * 32, "ci": "a" * 32}},
        ]
        result = handler.create_gear(
            hostname="sw1", ip="10.10.20.175", serial="CML12345UAD",
            platform="C9KV-UADP-8P", software="17.12.1prd9",
            mac="52:54:00:02:19:54", catalyst_id="device-1", confirm=True,
        )
        self.assertTrue(result["results"]["created"])
        self.assertEqual(request_mock.call_args_list[0].args[:2], ("POST", handler.CI_TABLE))
        self.assertEqual(request_mock.call_args_list[1].args[:2], ("POST", handler.ASSET_TABLE))

    @patch.object(handler, "_request", return_value={"result": {"sys_id": "a" * 32}})
    @patch.object(handler, "_find_ci")
    def test_update_rejects_unmanaged_ci(self, find_mock, request_mock) -> None:
        find_mock.return_value = [{"sys_id": "a" * 32, "correlation_id": "other"}]
        result = handler.handle_command(
            "update-gear", sys_id="a" * 32, software="17.12.2", confirm=True
        )
        self.assertEqual(result["status"], "error")
        request_mock.assert_not_called()

    @patch.object(handler, "_find_ci")
    def test_update_requires_exactly_one_field(self, find_mock) -> None:
        find_mock.return_value = [{
            "sys_id": "a" * 32,
            "correlation_id": f"{handler.MANAGED_PREFIX}device-1",
        }]
        result = handler.handle_command(
            "update-gear", sys_id="a" * 32, software="17.12.2",
            platform="C9KV-NEW", confirm=True,
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("exactly one", result["next_steps"][0])

    @patch.object(handler, "_request")
    @patch.object(handler, "_reference_sys_id", side_effect=["b" * 32, "c" * 32])
    @patch.object(handler, "_find_assets", return_value=[])
    @patch.object(handler, "_find_ci")
    def test_create_repairs_missing_asset_without_recreating_ci(
        self, find_mock, assets_mock, reference_mock, request_mock
    ) -> None:
        find_mock.return_value = [{
            "sys_id": "a" * 32,
            "serial_number": "SERIAL1",
            "correlation_id": f"{handler.MANAGED_PREFIX}device-1",
        }]
        request_mock.return_value = {"result": {"sys_id": "d" * 32, "ci": "a" * 32}}
        result = handler.create_gear(
            hostname="sw1", ip="10.10.20.175", serial="SERIAL1",
            platform="C9KV-UADP-8P", software="17.12.1",
            catalyst_id="device-1", confirm=True,
        )
        self.assertTrue(result["results"]["created"])
        self.assertFalse(result["results"]["ci_created"])
        self.assertEqual(request_mock.call_args.args[:2], ("POST", handler.ASSET_TABLE))

    @patch.object(handler, "_request")
    @patch.object(handler, "_find_assets")
    @patch.object(handler, "_find_ci")
    def test_delete_removes_asset_before_ci(self, find_mock, assets_mock, request_mock) -> None:
        find_mock.return_value = [{
            "sys_id": "a" * 32,
            "correlation_id": f"{handler.MANAGED_PREFIX}device-1",
        }]
        assets_mock.return_value = [{"sys_id": "b" * 32, "comments": handler.ASSET_MARKER}]
        result = handler.delete_gear(sys_id="a" * 32, confirm=True)
        self.assertTrue(result["results"]["deleted"])
        self.assertEqual(
            request_mock.call_args_list,
            [call("DELETE", handler.ASSET_TABLE, "b" * 32), call("DELETE", handler.CI_TABLE, "a" * 32)],
        )


class DeterministicImportTests(unittest.TestCase):
    INVENTORY = [
        {
            "hostname": "sw2", "managementIpAddress": "10.10.20.176",
            "serialNumber": "SERIAL2", "platformId": "C9KV-UADP-8P",
            "softwareVersion": "17.12.1", "macAddress": "52:54:00:00:00:02",
            "type": "Virtual switch", "id": "device-2",
        },
        {
            "hostname": "sw1", "managementIpAddress": "10.10.20.175",
            "serialNumber": "SERIAL1", "platformId": "C9KV-UADP-8P",
            "softwareVersion": "17.12.1", "macAddress": "52:54:00:00:00:01",
            "type": "Virtual switch", "id": "device-1",
        },
    ]

    @patch.object(handler, "_catalyst_inventory")
    def test_dry_run_is_sorted_and_makes_no_changes(self, inventory_mock) -> None:
        inventory_mock.return_value = list(self.INVENTORY)
        result = handler.import_catalyst(confirm=False)
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])
        self.assertEqual(
            [item["hostname"] for item in result["results"]["plan"]],
            ["sw1", "sw2"],
        )

    @patch.object(handler, "create_gear")
    @patch.object(handler, "_catalyst_inventory")
    def test_confirmed_import_reports_idempotent_outcomes(
        self, inventory_mock, create_mock
    ) -> None:
        inventory_mock.return_value = list(self.INVENTORY)
        create_mock.side_effect = [
            {"status": "success", "results": {"created": False}, "next_steps": []},
            {"status": "success", "results": {"created": False}, "next_steps": []},
        ]
        result = handler.import_catalyst(confirm=True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"]["created_count"], 0)
        self.assertEqual(result["results"]["unchanged_count"], 2)
        self.assertEqual(
            [item["hostname"] for item in result["results"]["outcomes"]],
            ["sw1", "sw2"],
        )

    @patch.object(handler, "create_gear")
    @patch.object(handler, "_catalyst_inventory")
    def test_invalid_inventory_blocks_all_mutations(self, inventory_mock, create_mock) -> None:
        inventory_mock.return_value = [{"hostname": "sw1", "managementIpAddress": "10.10.20.175"}]
        result = handler.import_catalyst(confirm=True)
        self.assertEqual(result["status"], "error")
        create_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

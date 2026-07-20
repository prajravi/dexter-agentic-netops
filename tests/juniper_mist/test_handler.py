#!/usr/bin/env python3
"""Tests for guarded Juniper Mist organization and provisioning workflows."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import requests

from tests.load_handler import load_handler

handler = load_handler("juniper-mist")

ORG_ID = "11111111-1111-4111-8111-111111111111"
SITE_ID = "22222222-2222-4222-8222-222222222222"
SITE = {
    "id": SITE_ID,
    "name": "Bengaluru Lab",
    "country_code": "IN",
    "timezone": "Asia/Kolkata",
    "address": "Bengaluru, Karnataka, India",
    "notes": handler.DEXTER_MARKER,
}


class ConfigurationTests(unittest.TestCase):
    @patch.object(handler, "_load_environment")
    def test_accepts_gc4_configuration(self, load_mock) -> None:
        values = {
            "MIST_API_HOST": "https://api.gc4.mist.com",
            "MIST_ALLOWED_HOST": "api.gc4.mist.com",
            "MIST_ORG_ID": ORG_ID,
            "MIST_API_TOKEN": "secret-token",
        }
        with patch.dict(os.environ, values, clear=True):
            api, org_id, token = handler._load_configuration()
        self.assertEqual(api, "https://api.gc4.mist.com")
        self.assertEqual(org_id, ORG_ID)
        self.assertTrue(token)

    @patch.object(handler, "_load_environment")
    def test_prefers_separate_read_and_write_tokens(self, load_mock) -> None:
        values = {
            "MIST_API_HOST": "https://api.gc4.mist.com",
            "MIST_ALLOWED_HOST": "api.gc4.mist.com",
            "MIST_ORG_ID": ORG_ID,
            "MIST_API_TOKEN": "fallback-token",
            "MIST_READ_TOKEN": "read-token",
            "MIST_WRITE_TOKEN": "write-token",
        }
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(handler._load_configuration()[2], "read-token")
            self.assertEqual(handler._load_configuration(for_write=True)[2], "write-token")

    @patch.object(handler, "_load_environment")
    def test_rejects_host_mismatch(self, load_mock) -> None:
        values = {
            "MIST_API_HOST": "https://api.mist.com",
            "MIST_ALLOWED_HOST": "api.gc4.mist.com",
            "MIST_ORG_ID": ORG_ID,
            "MIST_API_TOKEN": "secret-token",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(ValueError, "exactly match"):
                handler._load_configuration()

    def test_path_allowlist_restricts_org_and_methods(self) -> None:
        self.assertTrue(handler._allowed_path("GET", f"/api/v1/orgs/{ORG_ID}/sites", ORG_ID))
        self.assertTrue(handler._allowed_path("POST", f"/api/v1/orgs/{ORG_ID}/sites", ORG_ID))
        self.assertFalse(handler._allowed_path("DELETE", f"/api/v1/orgs/{ORG_ID}/sites", ORG_ID))
        self.assertFalse(handler._allowed_path("GET", "/api/v1/orgs/00000000-0000-4000-8000-000000000000/sites", ORG_ID))


class ReadWorkflowTests(unittest.TestCase):
    @patch.object(handler, "_request")
    @patch.object(handler, "_load_configuration", return_value=("https://api.gc4.mist.com", ORG_ID, "token"))
    def test_show_organization_filters_privileges(self, config_mock, request_mock) -> None:
        request_mock.side_effect = [
            {"privileges": [{"org_id": ORG_ID, "scope": "org", "role": "admin"}, {"org_id": "other", "role": "admin"}]},
            {"id": ORG_ID, "name": "Dexter Organization"},
        ]
        result = handler.show_organization()
        self.assertTrue(result["results"]["configured_org_id_matches"])
        self.assertEqual(len(result["results"]["token_privileges"]), 1)

    @patch.object(handler, "_paged_get")
    @patch.object(handler, "_load_configuration", return_value=("https://api.gc4.mist.com", ORG_ID, "token"))
    def test_inventory_summary_groups_devices(self, config_mock, paged_mock) -> None:
        paged_mock.side_effect = [
            ([{"type": "ap", "model": "AP45", "site_id": SITE_ID, "connected": True}], True),
            ([SITE], True),
        ]
        result = handler.inventory_summary()
        self.assertEqual(result["results"]["device_count"], 1)
        self.assertEqual(result["results"]["by_site"], [{"site": "Bengaluru Lab", "count": 1}])

    @patch.object(handler, "_request")
    def test_pagination_stops_on_repeated_page(self, request_mock) -> None:
        request_mock.return_value = [{"id": str(index)} for index in range(handler.PAGE_SIZE)]
        with self.assertRaisesRegex(ValueError, "repeated a page"):
            handler._paged_get("/api/v1/test", max_records=200)

    @patch.object(handler, "_request")
    @patch.object(handler, "_load_configuration", return_value=("https://api.gc4.mist.com", ORG_ID, "token"))
    def test_show_organization_rejects_id_mismatch(self, config_mock, request_mock) -> None:
        request_mock.side_effect = [
            {"privileges": [{"org_id": ORG_ID, "scope": "org", "role": "admin"}]},
            {"id": "33333333-3333-4333-8333-333333333333", "name": "Wrong Org"},
        ]
        with self.assertRaisesRegex(ValueError, "does not match"):
            handler.show_organization()


class SiteWorkflowTests(unittest.TestCase):
    @patch.object(handler, "_site_by_name", return_value=None)
    def test_site_plan_is_non_mutating(self, site_mock) -> None:
        result = handler.create_site_plan("Bengaluru Lab", "IN", "Asia/Kolkata", "Bengaluru, Karnataka, India")
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["results"]["action"], "create")
        self.assertTrue(result["results"]["confirmation_token"].startswith("dexter:create-site:"))

    @patch.object(handler, "_site_by_name", return_value={**SITE, "notes": "Created manually"})
    def test_site_plan_blocks_unmanaged_name_collision(self, site_mock) -> None:
        result = handler.create_site_plan("Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["results"]["action"], "blocked")
        self.assertNotIn("confirmation_token", result["results"])

    @patch.object(handler, "_site_by_name", return_value=SITE)
    def test_matching_site_is_unchanged_without_confirmation_token(self, site_mock) -> None:
        result = handler.create_site_plan("Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"]["action"], "unchanged")
        self.assertNotIn("confirmation_token", result["results"])

    @patch.object(handler, "_site_by_name", return_value=None)
    def test_site_create_requires_exact_confirmation(self, site_mock) -> None:
        result = handler.create_site("Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"], confirm=True, confirm_target="wrong")
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])

    @patch.object(handler, "_request")
    @patch.object(handler, "_load_configuration", return_value=("https://api.gc4.mist.com", ORG_ID, "token"))
    @patch.object(handler, "_site_by_name")
    def test_confirmed_site_is_created_and_verified(self, site_mock, config_mock, request_mock) -> None:
        site_mock.side_effect = [None, SITE]
        preview = handler.create_site_plan("Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"])
        site_mock.side_effect = [None, SITE]
        result = handler.create_site(
            "Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"], True,
            preview["results"]["confirmation_token"],
        )
        self.assertTrue(result["results"]["created"])
        self.assertTrue(result["results"]["verified"])
        request_mock.assert_called_once()

    @patch.object(handler, "_request", side_effect=requests.ConnectionError("connection lost"))
    @patch.object(handler, "_load_configuration", return_value=("https://api.gc4.mist.com", ORG_ID, "token"))
    @patch.object(handler, "_site_by_name")
    def test_ambiguous_site_write_is_verified_without_retry(self, site_mock, config_mock, request_mock) -> None:
        site_mock.side_effect = [None, SITE]
        token = handler._confirmation_token("create-site", handler._validate_site_inputs("Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"]))
        result = handler.create_site("Bengaluru Lab", "IN", "Asia/Kolkata", SITE["address"], True, token)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["results"]["created"], "unknown")
        self.assertTrue(result["results"]["verified"])
        request_mock.assert_called_once()


class WlanWorkflowTests(unittest.TestCase):
    def _psk_env(self):
        return patch.dict(os.environ, {"MIST_WLAN_PSK": "LabPassphrase-123"}, clear=False)

    @patch.object(handler, "_site_wlans", return_value=[])
    @patch.object(handler, "_resolve_site", return_value=SITE)
    def test_wlan_plan_hides_psk_and_stays_disabled(self, site_mock, wlans_mock) -> None:
        with self._psk_env():
            result = handler.create_wlan_plan("Bengaluru Lab", "Dexter-Employee")
        serialized = json.dumps(result)
        self.assertNotIn("LabPassphrase-123", serialized)
        self.assertFalse(result["results"]["configuration"]["enabled"])
        self.assertTrue(result["results"]["configuration"]["psk_configured"])

    @patch.object(handler, "_site_wlans", return_value=[])
    @patch.object(handler, "_resolve_site", return_value=SITE)
    def test_wlan_create_requires_confirmation(self, site_mock, wlans_mock) -> None:
        with self._psk_env():
            result = handler.create_wlan("Bengaluru Lab", "Dexter-Employee", confirm=False)
        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["created"])

    @patch.object(handler, "_request")
    @patch.object(handler, "_site_wlans")
    @patch.object(handler, "_resolve_site", return_value=SITE)
    def test_confirmed_wlan_is_created_disabled_and_verified(self, site_mock, wlans_mock, request_mock) -> None:
        created = {
            "id": "bb0159e5-be97-484c-9b86-1f58dfe690a0",
            "ssid": "Dexter-Employee",
            "enabled": False,
            "hide_ssid": False,
            "bands": ["24", "5"],
            "auth": {"type": "psk"},
        }
        wlans_mock.side_effect = [[], [], [created]]
        with self._psk_env():
            preview = handler.create_wlan_plan("Bengaluru Lab", "Dexter-Employee")
            result = handler.create_wlan(
                "Bengaluru Lab", "Dexter-Employee", "psk", True,
                preview["results"]["confirmation_token"],
            )
        self.assertTrue(result["results"]["verified"])
        self.assertFalse(result["results"]["wlan"]["enabled"])
        request_mock.assert_called_once()

    @patch.object(handler, "_site_wlans")
    @patch.object(handler, "_resolve_site", return_value=SITE)
    def test_wlan_conflict_is_blocked(self, site_mock, wlans_mock) -> None:
        wlans_mock.return_value = [{"ssid": "Dexter-Employee", "enabled": True, "auth": {"type": "open"}}]
        with self._psk_env():
            result = handler.create_wlan_plan("Bengaluru Lab", "Dexter-Employee")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["results"]["action"], "blocked")
        self.assertNotIn("confirmation_token", result["results"])

    @patch.object(handler, "_request", side_effect=requests.ConnectionError("connection lost"))
    @patch.object(handler, "_site_wlans")
    @patch.object(handler, "_resolve_site", return_value=SITE)
    def test_ambiguous_wlan_write_is_verified_without_retry(self, site_mock, wlans_mock, request_mock) -> None:
        created = {
            "id": "44444444-4444-4444-8444-444444444444",
            "ssid": "Dexter-Employee", "enabled": False, "hide_ssid": False,
            "bands": ["5", "24"], "auth": {"type": "psk"},
        }
        wlans_mock.side_effect = [[], [created]]
        with self._psk_env():
            payload, public = handler._wlan_payload("Dexter-Employee", "psk")
            token_plan = {"site_id": SITE_ID, **public}
            token = handler._confirmation_token("create-wlan", token_plan)
            result = handler.create_wlan("Bengaluru Lab", "Dexter-Employee", "psk", True, token)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["results"]["created"], "unknown")
        self.assertTrue(result["results"]["verified"])
        request_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

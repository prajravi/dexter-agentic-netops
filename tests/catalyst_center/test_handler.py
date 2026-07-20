"""Unit tests for deterministic routing and port-bounce safety guards."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tests.load_handler import load_handler

handler = load_handler("catalyst-center")


class DeviceResolutionTests(unittest.TestCase):
    def test_rejects_conflicting_hostname_and_device_id(self) -> None:
        api = Mock()
        api.devices.get_device_by_id.return_value = {
            "response": {"id": "device-1", "hostname": "switch-02.example.com"}
        }

        with self.assertRaisesRegex(ValueError, "does not match"):
            handler._resolve_device_id(api, "switch-01", "device-1")

    def test_rejects_ambiguous_hostname(self) -> None:
        api = Mock()
        api.devices.get_device_list.return_value = {
            "response": [
                {"id": "one", "hostname": "edge.example.com"},
                {"id": "two", "hostname": "edge.example.net"},
            ]
        }

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            handler._resolve_device_id(api, "edge", None)


class InterfaceMatchingTests(unittest.TestCase):
    def test_common_ios_abbreviation_matches(self) -> None:
        interfaces = [{"portName": "GigabitEthernet1/0/5", "id": "port-5"}]
        self.assertEqual(handler._find_interface(interfaces, "Gi1/0/5"), interfaces[0])


class PortBounceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = Mock()
        self.interface = {
            "id": "port-5",
            "portName": "GigabitEthernet1/0/5",
            "interfaceType": "Physical",
            "portMode": "access",
            "adminStatus": "UP",
            "status": "up",
            "description": "Example endpoint",
        }
        self.api.devices.get_device_list.return_value = {
            "response": [{"id": "device-1", "hostname": "switch-01"}]
        }
        self.api.devices.get_device_interfaces_by_specified_range.return_value = {
            "response": [self.interface]
        }

    def test_plan_returns_target_specific_confirmation_token(self) -> None:
        with patch.object(handler, "_api", return_value=self.api):
            result = handler.port_bounce_plan(hostname="switch-01", interface="Gi1/0/5")

        self.assertEqual(result["status"], "warning")
        self.assertEqual(
            result["results"]["confirmation_token"],
            "device-1:GigabitEthernet1/0/5",
        )

    def test_bounce_rejects_wrong_confirmation_without_write(self) -> None:
        with patch.object(handler, "_api", return_value=self.api), patch.object(
            handler, "_put"
        ) as put:
            result = handler.port_bounce(
                hostname="switch-01",
                interface="Gi1/0/5",
                confirm_target="wrong-token",
            )

        self.assertEqual(result["error_code"], "CONFIRMATION_REQUIRED")
        put.assert_not_called()

    def test_bounce_rejects_already_down_port(self) -> None:
        self.interface["adminStatus"] = "DOWN"
        token = "device-1:GigabitEthernet1/0/5"
        with patch.object(handler, "_api", return_value=self.api), patch.object(
            handler, "_put"
        ) as put:
            result = handler.port_bounce(
                hostname="switch-01",
                interface="Gi1/0/5",
                confirm_target=token,
            )

        self.assertEqual(result["error_code"], "PORT_ALREADY_DOWN")
        put.assert_not_called()

    def test_restore_failure_is_structured_as_critical(self) -> None:
        token = "device-1:GigabitEthernet1/0/5"
        down = {"status": "success", "task_id": "down-task"}
        up = {"status": "error", "task_id": "up-task"}
        with patch.object(handler, "_api", return_value=self.api), patch.object(
            handler, "_set_access_port_admin_state", side_effect=[down, up]
        ), patch.object(handler.time, "sleep"):
            result = handler.port_bounce(
                hostname="switch-01",
                interface="Gi1/0/5",
                confirm_target=token,
            )

        self.assertEqual(result["error_code"], "PORT_RESTORE_FAILED")
        self.assertTrue(result["results"]["critical"])


if __name__ == "__main__":
    unittest.main()

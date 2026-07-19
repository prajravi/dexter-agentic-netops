#!/usr/bin/env python3
"""Unit tests for the read-only ServiceNow development handler."""

from __future__ import annotations

import unittest
from unittest.mock import call, patch

from tests.load_handler import load_handler

handler = load_handler("servicenow-query")


class ValidationTests(unittest.TestCase):
    def test_rejects_script_bearing_query(self) -> None:
        with self.assertRaisesRegex(ValueError, "not permitted"):
            handler._validate_query("active=true^sys_id=javascript:gs.getUserID()")

    def test_rejects_invalid_table_identifier(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid table"):
            handler.query_table("incident;DELETE")

    def test_rejects_invalid_bulk_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "--limit"):
            handler.list_all_records("incident", page_size=101)
        with self.assertRaisesRegex(ValueError, "--max-records"):
            handler.list_all_records("incident", max_records=0)


class QueryTests(unittest.TestCase):
    @patch.object(handler, "_request", return_value={"result": [{"number": "INC1"}]})
    def test_query_table_builds_read_only_page(self, request_mock) -> None:
        result = handler.query_table(
            "incident",
            fields="number,state",
            order_by="number",
            limit=25,
            offset=50,
            display_values=True,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"]["count"], 1)
        path, params = request_mock.call_args.args
        self.assertEqual(path, "/api/now/table/incident")
        self.assertEqual(params["sysparm_limit"], 25)
        self.assertEqual(params["sysparm_offset"], 50)
        self.assertEqual(params["sysparm_fields"], "number,state")
        self.assertEqual(params["sysparm_query"], "ORDERBYnumber")
        self.assertEqual(params["sysparm_display_value"], "true")

    @patch.object(handler, "query_table")
    def test_list_all_records_paginates_until_short_page(self, query_mock) -> None:
        query_mock.side_effect = [
            {"results": {"records": [{"number": "INC1"}, {"number": "INC2"}]}},
            {"results": {"records": [{"number": "INC3"}]}},
        ]

        result = handler.list_all_records(
            "incident", fields="number", order_by="number", page_size=2
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["results"]["complete"])
        self.assertEqual(result["results"]["count"], 3)
        self.assertEqual(
            query_mock.call_args_list,
            [
                call(
                    table="incident", query=None, fields="number", order_by="number",
                    limit=2, offset=0, display_values=False,
                ),
                call(
                    table="incident", query=None, fields="number", order_by="number",
                    limit=2, offset=2, display_values=False,
                ),
            ],
        )

    @patch.object(handler, "query_table")
    def test_list_all_records_warns_at_cap(self, query_mock) -> None:
        query_mock.return_value = {
            "results": {"records": [{"number": "INC1"}, {"number": "INC2"}]}
        }

        result = handler.list_all_records("incident", page_size=2, max_records=2)

        self.assertEqual(result["status"], "warning")
        self.assertFalse(result["results"]["complete"])
        self.assertEqual(result["results"]["count"], 2)
        self.assertEqual(result["results"]["continuation_offset"], 2)

    @patch.object(handler, "list_all_records")
    def test_summarize_table_groups_blank_and_display_values(self, list_mock) -> None:
        list_mock.return_value = {
            "status": "success",
            "results": {
                "table": "task",
                "count": 4,
                "complete": True,
                "records": [
                    {"sys_class_name": "Incident"},
                    {"sys_class_name": "Incident"},
                    {"sys_class_name": "Problem"},
                    {"sys_class_name": ""},
                ],
            },
            "next_steps": ["All matching records were returned."],
        }

        result = handler.summarize_table("task", "sys_class_name")

        self.assertEqual(
            result["results"]["groups"],
            [
                {"value": "(blank)", "count": 1},
                {"value": "Incident", "count": 2},
                {"value": "Problem", "count": 1},
            ],
        )

    def test_describe_record_types_is_explicit(self) -> None:
        result = handler.describe_record_types()
        names = [item["record_type"] for item in result["results"]["record_types"]]
        self.assertIn("incidents", names)
        self.assertIn("network-gear", names)
        for item in result["results"]["record_types"]:
            self.assertTrue(item["table"])
            self.assertTrue(item["fields"])

    @patch.object(handler, "list_all_records")
    def test_list_records_uses_fixed_preset(self, list_mock) -> None:
        list_mock.return_value = {
            "status": "success",
            "results": {"records": [], "complete": True},
            "next_steps": [],
        }
        result = handler.list_records("incidents")
        self.assertEqual(result["results"]["record_type"], "incidents")
        self.assertEqual(list_mock.call_args.kwargs["table"], "incident")
        self.assertIn("number", list_mock.call_args.kwargs["fields"])
        self.assertEqual(list_mock.call_args.kwargs["order_by"], "number")

    def test_invalid_record_type_is_actionable(self) -> None:
        with self.assertRaisesRegex(ValueError, "incidents"):
            handler.list_records("incident-records")


if __name__ == "__main__":
    unittest.main()

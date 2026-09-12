import concurrent.futures as futures
import copy
import datetime
import http.client
import inspect
import json
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import backend


CONTRACT_CONTENT = {
    "product_name": "SentinelMesh",
    "tagline": "Backend test fixture",
    "scope_note": "Local demo database and inbox",
    "pitch_summary": "Synthetic contract seeds, not the teammate presentation module.",
    "scenarios": [
        {"scenario_id": "missing_registration", "title": "Missing registration",
         "description": "Test fixture", "payment_status": "confirmed",
         "registration_exists": False, "ticket_exists": False,
         "delivery_exists": False, "lose_ticket_response_once": False},
        {"scenario_id": "delivery_failed", "title": "Delivery failed",
         "description": "Test fixture", "payment_status": "confirmed",
         "registration_exists": True, "ticket_exists": True,
         "delivery_exists": False, "lose_ticket_response_once": False},
        {"scenario_id": "lost_response", "title": "Lost response",
         "description": "Test fixture", "payment_status": "confirmed",
         "registration_exists": True, "ticket_exists": False,
         "delivery_exists": False, "lose_ticket_response_once": True},
        {"scenario_id": "payment_unconfirmed", "title": "Payment unconfirmed",
         "description": "Test fixture", "payment_status": "pending",
         "registration_exists": False, "ticket_exists": False,
         "delivery_exists": False, "lose_ticket_response_once": False},
    ],
}

SNAPSHOT_KEYS = {
    "case_id", "title", "status", "payment_id", "payment_status", "registration_id",
    "ticket_id", "ticket_token", "delivery_status", "registration_count",
    "ticket_count", "delivery_count", "receipt",
}
RECEIPT_KEYS = {"case_id", "payment_id", "ticket_id", "verified_at", "checks", "scope"}
AUDIT_KEYS = {"timestamp", "type", "summary", "tool", "ok", "data"}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sentinelmesh-test-")
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(pathlib.Path(self.temp.name) / "test.sqlite3")
        self.backend = backend.SMBackend(self.db_path, CONTRACT_CONTENT)
        self.addCleanup(self.backend.close)
        self.backend.start()

    def assert_envelope(self, result):
        self.assertEqual(set(result), {"ok", "data", "error"})
        self.assertIs(type(result["ok"]), bool)
        self.assertIs(type(result["data"]), dict)
        if result["ok"]:
            self.assertIsNone(result["error"])
        else:
            self.assertEqual(set(result["error"]), {"code", "message", "retryable"})
            self.assertIs(type(result["error"]["code"]), str)
            self.assertTrue(result["error"]["code"])
            self.assertIs(type(result["error"]["message"]), str)
            self.assertIs(type(result["error"]["retryable"]), bool)

    def tool(self, case_id, name, args=None):
        result = self.backend.call_tool(case_id, name, {} if args is None else args)
        self.assert_envelope(result)
        return result

    def ok_tool(self, case_id, name, args=None):
        result = self.tool(case_id, name, args)
        self.assertTrue(result["ok"], result)
        return result["data"]

    def assert_utc(self, timestamp):
        parsed = datetime.datetime.fromisoformat(timestamp)
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(0))

    def recover_for_test(self, case_id):
        """Test-only driver based on observations, not an implementation of agent.py."""
        snapshot = self.ok_tool(case_id, "inspect_workflow")
        for _ in range(8):
            if snapshot["payment_status"] != "confirmed":
                return self.ok_tool(case_id, "escalate", {"reason": "Payment is pending."})
            if not snapshot["registration_id"]:
                name = "ensure_registration"
            elif not snapshot["ticket_id"]:
                name = "ensure_ticket"
            elif snapshot["delivery_status"] != "delivered":
                name = "deliver_ticket"
            else:
                return self.ok_tool(case_id, "verify_completion")
            result = self.tool(case_id, name)
            if not result["ok"]:
                self.assertEqual(result["error"]["code"], "OUTCOME_UNKNOWN")
            snapshot = self.ok_tool(case_id, "inspect_workflow")
        self.fail("Test recovery exceeded eight iterations.")

    def raw_http(self, body, content_type="application/json"):
        connection = http.client.HTTPConnection(*self.backend._server.server_address, timeout=5)
        try:
            connection.request("POST", "/tool", body=body, headers={"Content-Type": content_type})
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assert_envelope(result)
            return response.status, result
        finally:
            connection.close()

    def sql(self, statement, parameters=(), foreign_keys=True):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("PRAGMA foreign_keys=" + ("ON" if foreign_keys else "OFF"))
            cursor = connection.execute(statement, parameters)
            rows = cursor.fetchall()
            connection.commit()
            return rows
        finally:
            connection.close()

    def test_exact_seed_observations_and_summary_schemas(self):
        expected = {
            "missing_registration": ("confirmed", 0, 0, 0),
            "delivery_failed": ("confirmed", 1, 1, 0),
            "lost_response": ("confirmed", 1, 0, 0),
            "payment_unconfirmed": ("pending", 0, 0, 0),
        }
        for scenario, state in expected.items():
            with self.subTest(scenario=scenario):
                case_id = self.backend.seed_scenario(scenario)
                snapshot = self.ok_tool(case_id, "inspect_workflow")
                self.assertEqual(set(snapshot), SNAPSHOT_KEYS)
                self.assertEqual(snapshot, self.backend.get_snapshot(case_id))
                self.assertEqual(tuple(snapshot[key] for key in (
                    "payment_status", "registration_count", "ticket_count", "delivery_count",
                )), state)
                self.assertEqual(snapshot["status"], "OPEN")
                self.assertIsNone(snapshot["receipt"])
                self.assertNotIn("scenario_id", snapshot)
                self.assertNotIn("lose_ticket_response_once", snapshot)
        self.assertEqual(len(self.backend.list_cases()), 4)
        for case in self.backend.list_cases():
            self.assertEqual(set(case), {"case_id", "title", "status"})

    def test_new_seed_has_fresh_ids_and_injected_content(self):
        content = copy.deepcopy(CONTRACT_CONTENT)
        content["scenarios"][0]["scenario_id"] = "injected-id"
        content["scenarios"][0]["title"] = "Injected title"
        other = backend.SMBackend(str(pathlib.Path(self.temp.name) / "injected.db"), content)
        self.addCleanup(other.close)
        content["scenarios"][0]["payment_status"] = "pending"
        cases = [other.seed_scenario("injected-id") for _ in range(2)]
        snapshots = [other.get_snapshot(case) for case in cases]
        self.assertNotEqual(cases[0], cases[1])
        self.assertNotEqual(snapshots[0]["payment_id"], snapshots[1]["payment_id"])
        self.assertEqual(snapshots[0]["title"], "Injected title")
        self.assertEqual(snapshots[0]["payment_status"], "confirmed")

    def test_recover_three_cases_verify_receipts_tokens_and_reruns(self):
        for scenario in ("missing_registration", "delivery_failed", "lost_response"):
            with self.subTest(scenario=scenario):
                case_id = self.backend.seed_scenario(scenario)
                payment_id = self.backend.get_snapshot(case_id)["payment_id"]
                verified = self.recover_for_test(case_id)
                self.assertEqual(set(verified), {"complete", "checks", "receipt"})
                self.assertIs(verified["complete"], True)
                receipt = verified["receipt"]
                self.assertEqual(set(receipt), RECEIPT_KEYS)
                self.assertEqual(receipt["scope"], "Local demo database and inbox")
                self.assertEqual(receipt["case_id"], case_id)
                self.assertEqual(receipt["payment_id"], payment_id)
                self.assertEqual(len(verified["checks"]), 5)
                for check in verified["checks"]:
                    self.assertEqual(set(check), {"name", "passed", "evidence"})
                    self.assertIs(check["passed"], True)
                    self.assertIs(type(check["evidence"]), str)
                self.assert_utc(receipt["verified_at"])
                before = self.backend.get_snapshot(case_id)
                self.assertEqual(before["status"], "RESOLVED")
                self.assertEqual(before["receipt"], receipt)
                self.assertEqual([before[key] for key in (
                    "registration_count", "ticket_count", "delivery_count",
                )], [1, 1, 1])
                self.assertEqual(receipt["ticket_id"], before["ticket_id"])
                self.assertEqual(self.backend.verify_token(before["ticket_token"]), {
                    "valid": True, "case_id": case_id, "ticket_id": before["ticket_id"],
                })
                for name in ("ensure_registration", "ensure_ticket", "deliver_ticket"):
                    self.assertIs(self.ok_tool(case_id, name)["created"], False)
                self.assertEqual(self.recover_for_test(case_id)["receipt"], receipt)
                self.assertEqual(self.backend.get_snapshot(case_id), before)

    def test_pending_payment_rejects_all_downstream_tools_and_escalates(self):
        case_id = self.backend.seed_scenario("payment_unconfirmed")
        payment_id = self.backend.get_snapshot(case_id)["payment_id"]
        for name in ("ensure_registration", "ensure_ticket", "deliver_ticket"):
            result = self.tool(case_id, name)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], {
                "code": "PAYMENT_NOT_CONFIRMED", "message": "Payment is pending.", "retryable": False,
            })
        self.assertEqual(self.recover_for_test(case_id)["status"], "NEEDS_REVIEW")
        verified = self.ok_tool(case_id, "verify_completion")
        self.assertFalse(verified["complete"])
        self.assertIsNone(verified["receipt"])
        snapshot = self.backend.get_snapshot(case_id)
        self.assertEqual(snapshot["payment_status"], "pending")
        self.assertEqual(snapshot["payment_id"], payment_id)
        self.assertEqual(snapshot["status"], "NEEDS_REVIEW")
        self.assertEqual([snapshot[key] for key in (
            "registration_count", "ticket_count", "delivery_count",
        )], [0, 0, 0])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "PAYMENT_NOT_CONFIRMED"):
            self.sql("INSERT INTO registrations VALUES (?,?,?,?)", ("REG-forbidden", payment_id, case_id, "now"))

    def test_lost_response_commit_flag_audit_and_restart(self):
        case_id = self.backend.seed_scenario("lost_response")
        result = self.tool(case_id, "ensure_ticket")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertTrue(result["error"]["retryable"])
        self.backend.close()
        self.backend = backend.SMBackend(self.db_path, CONTRACT_CONTENT)
        self.addCleanup(self.backend.close)
        self.backend.start()
        snapshot = self.ok_tool(case_id, "inspect_workflow")
        self.assertEqual(snapshot["ticket_count"], 1)
        self.assertIsNotNone(snapshot["ticket_id"])
        events = self.backend.get_audit(case_id)
        self.assertEqual([event["tool"] for event in events], ["ensure_ticket", "inspect_workflow"])
        self.assertEqual(events[0]["data"]["result"], result)
        self.assertEqual(events[1]["data"]["result"]["data"]["ticket_id"], snapshot["ticket_id"])
        retry = self.ok_tool(case_id, "ensure_ticket")
        self.assertFalse(retry["created"])
        self.assertEqual(retry["ticket_id"], snapshot["ticket_id"])
        self.assertEqual(self.sql("SELECT lose_ticket_response_once FROM cases WHERE case_id=?", (case_id,)), [(0,)])

    def test_concurrent_duplicates_across_two_http_servers_share_one_database(self):
        other = backend.SMBackend(self.db_path, CONTRACT_CONTENT)
        self.addCleanup(other.close)
        other.start()
        case_id = self.backend.seed_scenario("missing_registration")
        with futures.ThreadPoolExecutor(max_workers=12) as pool:
            for name, expected_keys in (
                ("ensure_registration", {"registration_id", "created"}),
                ("ensure_ticket", {"ticket_id", "ticket_token", "created"}),
                ("deliver_ticket", {"delivery_id", "created"}),
            ):
                jobs = [pool.submit((self.backend if i % 2 else other).call_tool, case_id, name, {}) for i in range(24)]
                results = [job.result(timeout=15) for job in jobs]
                for result in results:
                    self.assert_envelope(result)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(set(result["data"]), expected_keys)
                self.assertEqual(sum(result["data"]["created"] for result in results), 1)
        self.assertTrue(self.ok_tool(case_id, "verify_completion")["complete"])
        self.assertEqual(len(self.backend.get_audit(case_id)), 73)

    def test_concurrent_lost_response_is_consumed_once(self):
        case_id = self.backend.seed_scenario("lost_response")
        with futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.backend.call_tool(case_id, "ensure_ticket", {}), range(24)))
        failures = [result for result in results if not result["ok"]]
        self.assertEqual(len(failures), 1, results)
        self.assertEqual(failures[0]["error"]["code"], "OUTCOME_UNKNOWN")
        successes = [result for result in results if result["ok"]]
        self.assertTrue(all(result["data"]["created"] is False for result in successes))
        self.assertEqual(len({result["data"]["ticket_id"] for result in successes}), 1)
        self.assertEqual(self.ok_tool(case_id, "inspect_workflow")["ticket_count"], 1)

    def test_verification_incomplete_is_successful_inspection(self):
        case_id = self.backend.seed_scenario("missing_registration")
        result = self.ok_tool(case_id, "verify_completion")
        self.assertEqual(set(result), {"complete", "checks", "receipt"})
        self.assertFalse(result["complete"])
        self.assertIsNone(result["receipt"])
        self.assertEqual([check["passed"] for check in result["checks"]], [True, False, False, False, False])
        self.assertEqual(self.backend.get_snapshot(case_id)["status"], "OPEN")

    def test_only_verification_resolves_and_escalation_preserves_success(self):
        case_id = self.backend.seed_scenario("delivery_failed")
        self.ok_tool(case_id, "deliver_ticket")
        self.assertEqual(self.backend.get_snapshot(case_id)["status"], "OPEN")
        self.assertIsNone(self.backend.get_snapshot(case_id)["receipt"])
        self.assertEqual(self.ok_tool(case_id, "escalate", {"reason": "Check records."})["status"], "OPEN")
        receipt = self.ok_tool(case_id, "verify_completion")["receipt"]
        self.assertEqual(self.ok_tool(case_id, "escalate", {"reason": "Later agent error."})["status"], "RESOLVED")
        self.assertEqual(self.backend.get_snapshot(case_id)["receipt"], receipt)

    def test_each_independent_verification_condition_reads_source_records(self):
        corruptions = [
            ("payment_confirmed", "UPDATE payments SET status='pending' WHERE case_id=?"),
            ("one_matching_registration", "DELETE FROM registrations WHERE case_id=?"),
            ("one_matching_ticket", "DELETE FROM tickets WHERE case_id=?"),
            ("ticket_token_valid", "UPDATE tickets SET ticket_token='!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!' WHERE case_id=?"),
            ("one_matching_inbox_delivery", "UPDATE inbox_deliveries SET ticket_id='wrong-ticket' WHERE case_id=?"),
        ]
        for failed_check, sql in corruptions:
            with self.subTest(check=failed_check):
                case_id = self.backend.seed_scenario("delivery_failed")
                previous_receipt = self.recover_for_test(case_id)["receipt"]
                self.sql(sql, (case_id,), foreign_keys=False)  # Deliberate out-of-band corruption.
                result = self.ok_tool(case_id, "verify_completion")
                self.assertFalse(result["complete"])
                self.assertIsNone(result["receipt"])
                self.assertFalse({check["name"]: check["passed"] for check in result["checks"]}[failed_check])
                snapshot = self.backend.get_snapshot(case_id)
                self.assertEqual(snapshot["status"], "OPEN")
                self.assertEqual(snapshot["receipt"], previous_receipt)  # Historical only.

    def test_matching_counts_alone_cannot_validate_cross_case_ticket(self):
        case_id = self.backend.seed_scenario("delivery_failed")
        other_id = self.backend.seed_scenario("delivery_failed")
        self.ok_tool(case_id, "deliver_ticket")
        other_registration = self.backend.get_snapshot(other_id)["registration_id"]
        # Free the other registration's unique ticket link, then deliberately corrupt
        # the first case with a real registration from the wrong payment and case.
        self.sql("DELETE FROM tickets WHERE case_id=?", (other_id,))
        self.sql("UPDATE tickets SET registration_id=? WHERE case_id=?", (other_registration, case_id), foreign_keys=False)
        snapshot = self.backend.get_snapshot(case_id)
        self.assertEqual([snapshot[key] for key in ("registration_count", "ticket_count", "delivery_count")], [1, 1, 1])
        self.assertFalse(self.backend.verify_token(snapshot["ticket_token"])["valid"])
        self.assertFalse(self.ok_tool(case_id, "verify_completion")["complete"])
        self.assertEqual(self.tool(case_id, "deliver_ticket")["error"]["code"], "INVALID_TICKET")

    def test_sql_uniqueness_and_composite_foreign_keys(self):
        first = self.backend.seed_scenario("delivery_failed")
        second = self.backend.seed_scenario("delivery_failed")
        a, b = self.backend.get_snapshot(first), self.backend.get_snapshot(second)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO registrations VALUES (?,?,?,?)", ("duplicate", a["payment_id"], first, "now"))
        self.sql("DELETE FROM tickets WHERE case_id=?", (second,))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            self.sql("UPDATE tickets SET registration_id=? WHERE case_id=?", (b["registration_id"], first))
        self.ok_tool(first, "deliver_ticket")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO inbox_deliveries VALUES (?,?,?,?,?)", ("duplicate", a["ticket_id"], a["payment_id"], first, "now"))
        self.assertEqual(self.sql("PRAGMA foreign_key_check"), [])

    def test_unknown_tools_arguments_cases_and_audited_actual_results(self):
        case_id = self.backend.seed_scenario("missing_registration")
        attempts = [
            ("run_shell", {}, "UNKNOWN_TOOL"),
            ("set_payment_status", {"status": "confirmed"}, "UNKNOWN_TOOL"),
            ("inspect_workflow", {"url": "http://example.invalid"}, "INVALID_ARGUMENTS"),
            ("ensure_registration", {"payment_id": "other"}, "INVALID_ARGUMENTS"),
            ("ensure_ticket", {"case_id": "other"}, "INVALID_ARGUMENTS"),
            ("deliver_ticket", {"sql": "arbitrary query"}, "INVALID_ARGUMENTS"),
            ("verify_completion", {"complete": True}, "INVALID_ARGUMENTS"),
            ("ensure_registration", [], "INVALID_ARGUMENTS"),
            ("ensure_registration", "{}", "INVALID_ARGUMENTS"),
            ("escalate", {}, "INVALID_ARGUMENTS"),
            ("escalate", {"reason": " "}, "INVALID_ARGUMENTS"),
            ("escalate", {"reason": True}, "INVALID_ARGUMENTS"),
            ("escalate", {"reason": "Review.", "status": "RESOLVED"}, "INVALID_ARGUMENTS"),
        ]
        results = []
        for name, args, expected_code in attempts:
            with self.subTest(name=name, args=args):
                result = self.tool(case_id, name, args)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], expected_code)
                results.append(result)
        events = self.backend.get_audit(case_id)
        self.assertEqual(len(events), len(attempts))
        self.assertEqual([event["data"]["result"] for event in events], results)
        for event in events:
            self.assertEqual(set(event), AUDIT_KEYS)
            self.assertFalse(event["ok"])
            self.assert_utc(event["timestamp"])
        unknown = self.tool("CASE-unknown", "inspect_workflow")
        self.assertEqual(unknown["error"]["code"], "UNKNOWN_CASE")
        self.assertEqual(self.backend.get_audit("CASE-unknown")[0]["data"]["result"], unknown)
        self.assertEqual(self.sql("SELECT case_id FROM audit_events WHERE requested_case_id='CASE-unknown'"), [(None,)])
        self.assertEqual(self.backend.get_snapshot(case_id)["registration_count"], 0)

    def test_write_prerequisites_rejected_without_automatic_repair(self):
        case_id = self.backend.seed_scenario("missing_registration")
        self.assertEqual(self.tool(case_id, "ensure_ticket")["error"]["code"], "REGISTRATION_REQUIRED")
        self.assertEqual(self.tool(case_id, "deliver_ticket")["error"]["code"], "TICKET_REQUIRED")
        self.assertEqual(self.backend.get_snapshot(case_id)["registration_count"], 0)

    def test_http_strict_json_and_exact_request_schema(self):
        case_id = self.backend.seed_scenario("missing_registration")
        bodies = [
            b"{", b"[]", b"null", b'{"case_id":NaN}',
            b'{"case_id":"x","case_id":"y","name":"ensure_ticket","args":{}}',
            json.dumps({"case_id": case_id, "name": "ensure_registration", "args": {}, "extra": True}).encode(),
            json.dumps({"case_id": case_id, "name": "ensure_registration", "args": None}).encode(),
            json.dumps({"case_id": True, "name": "inspect_workflow", "args": {}}).encode(),
            b"x" * (backend.SM_BACKEND_MAX_REQUEST_BYTES + 1),
        ]
        for body in bodies:
            with self.subTest(body=body[:80]):
                _, result = self.raw_http(body)
                self.assertFalse(result["ok"])
        _, result = self.raw_http(b"{}", content_type="text/plain")
        self.assertFalse(result["ok"])
        self.assertEqual(self.backend.get_snapshot(case_id)["registration_count"], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM audit_events")[0][0], len(bodies) + 1)

    def test_nonserializable_and_oversize_calls_still_return_audited_errors(self):
        case_id = self.backend.seed_scenario("missing_registration")
        for args in ({"bad": object()}, {"bad": float("nan")}, {"bad": "x" * 20000}):
            result = self.tool(case_id, "ensure_registration", args)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "INVALID_REQUEST")
            self.assertEqual(self.backend.get_audit(case_id)[-1]["data"]["result"], result)

    def test_oversized_content_length_returns_audited_json_rejection(self):
        connection = http.client.HTTPConnection(*self.backend._server.server_address, timeout=5)
        try:
            connection.request("POST", "/tool", body=b"{}", headers={
                "Content-Type": "application/json", "Content-Length": "9" * 5000,
            })
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assert_envelope(result)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "INVALID_REQUEST")
            self.assertEqual(self.sql("SELECT count(*) FROM audit_events"), [(1,)])
        finally:
            connection.close()

    def test_real_http_timeout_returns_before_delayed_response_and_audits_error(self):
        case_id = self.backend.seed_scenario("missing_registration")
        release = threading.Event()
        original_send = backend.SM_BACKEND_HTTPHandler._send

        def delay_response(handler, status, result):
            release.wait(timeout=2)
            original_send(handler, status, result)

        try:
            with mock.patch.object(backend.SM_BACKEND_HTTPHandler, "_send", delay_response), \
                    mock.patch.object(backend, "SM_BACKEND_HTTP_TIMEOUT", 0.05):
                started = time.monotonic()
                result = self.tool(case_id, "ensure_registration")
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "TRANSPORT_ERROR")
                self.assertIn("timed out", result["error"]["message"])
                self.assertEqual(self.backend.get_audit(case_id)[-1]["data"]["result"], result)
        finally:
            release.set()
        self.assertEqual(self.ok_tool(case_id, "inspect_workflow")["registration_count"], 1)

    def test_real_disconnected_http_response_preserves_committed_write(self):
        case_id = self.backend.seed_scenario("missing_registration")
        original_send = backend.SM_BACKEND_HTTPHandler._send

        def disconnect(handler, status, result):
            if result["ok"] and "registration_id" in result["data"]:
                handler.connection.shutdown(socket.SHUT_RDWR)
                handler.connection.close()
                handler.close_connection = True
            else:
                original_send(handler, status, result)

        with mock.patch.object(backend.SM_BACKEND_HTTPHandler, "_send", disconnect):
            result = self.tool(case_id, "ensure_registration")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TRANSPORT_ERROR")
        self.assertTrue(result["error"]["retryable"])
        snapshot = self.ok_tool(case_id, "inspect_workflow")
        self.assertEqual(snapshot["registration_count"], 1)
        events = self.backend.get_audit(case_id)
        self.assertTrue(events[0]["data"]["result"]["ok"])
        self.assertEqual(events[1]["data"]["result"], result)
        self.assertEqual(events[2]["tool"], "inspect_workflow")
        self.assertFalse(self.ok_tool(case_id, "ensure_registration")["created"])

    def test_malformed_http_response_cannot_claim_success(self):
        case_id = self.backend.seed_scenario("missing_registration")
        original_send = backend.SM_BACKEND_HTTPHandler._send

        def invalid_envelope(handler, status, result):
            original_send(handler, status, {"ok": "true", "data": {}, "error": None})

        with mock.patch.object(backend.SM_BACKEND_HTTPHandler, "_send", invalid_envelope):
            result = self.tool(case_id, "inspect_workflow")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_RESPONSE")
        self.assertEqual(self.backend.get_audit(case_id)[-1]["data"]["result"], result)

    def test_transaction_rolls_back_ticket_and_flag_if_audit_commit_fails(self):
        case_id = self.backend.seed_scenario("lost_response")
        self.sql("""CREATE TRIGGER reject_test_ticket_audit BEFORE INSERT ON audit_events
                    WHEN NEW.tool='ensure_ticket'
                    BEGIN SELECT RAISE(ABORT,'deliberate audit failure'); END""")
        result = self.tool(case_id, "ensure_ticket")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "DATABASE_ERROR")
        self.assertIn("deliberate audit failure", result["error"]["message"])
        self.assertIn("Audit was not persisted", result["error"]["message"])
        self.sql("DROP TRIGGER reject_test_ticket_audit")
        self.assertEqual(self.ok_tool(case_id, "inspect_workflow")["ticket_count"], 0)
        self.assertEqual(self.sql("SELECT lose_ticket_response_once FROM cases WHERE case_id=?", (case_id,)), [(1,)])
        self.assertEqual(self.tool(case_id, "ensure_ticket")["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(self.ok_tool(case_id, "inspect_workflow")["ticket_count"], 1)

    def test_agent_event_schema_never_supplies_proof(self):
        case_id = self.backend.seed_scenario("missing_registration")
        event = {"type": "decision", "summary": "Model claims this case is resolved.", "tool": "verify_completion", "step": 2}
        self.backend.record_agent_event(case_id, event)
        saved = self.backend.get_audit(case_id)[0]
        self.assertEqual(set(saved), AUDIT_KEYS)
        self.assertEqual(saved["data"], {"step": 2})
        self.assertIsNone(saved["ok"])
        self.assertEqual(saved["summary"], event["summary"])
        self.assert_utc(saved["timestamp"])
        self.assertEqual(self.backend.get_snapshot(case_id)["status"], "OPEN")
        self.assertIsNone(self.backend.get_snapshot(case_id)["receipt"])
        for invalid in ({**event, "receipt": {}}, {**event, "step": True}, {**event, "step": -1}, {}):
            with self.assertRaises(ValueError):
                self.backend.record_agent_event(case_id, invalid)
        with self.assertRaises(ValueError):
            self.backend.record_agent_event("CASE-missing", event)
        self.assertEqual(len(self.backend.get_audit(case_id)), 1)

    def test_read_methods_and_token_checker_do_not_seed_repair_or_resolve(self):
        case_id = self.backend.seed_scenario("delivery_failed")
        before = self.backend.get_snapshot(case_id)
        audit = self.backend.get_audit(case_id)
        for _ in range(3):
            self.assertTrue(self.backend.verify_token(before["ticket_token"])["valid"])
            self.assertEqual(self.backend.get_snapshot(case_id), before)
            self.assertEqual(len(self.backend.list_cases()), 1)
        for token in (None, "", "PAY-whatever", "https://localhost/", "x" * 43, [], True):
            self.assertEqual(self.backend.verify_token(token), {"valid": False, "case_id": None, "ticket_id": None})
        self.assertEqual(self.backend.get_audit(case_id), audit)
        self.assertEqual(self.sql("SELECT count(*) FROM completion_receipts"), [(0,)])

    def test_unknown_snapshot_and_seed_raise_without_creating_case(self):
        with self.assertRaises(ValueError):
            self.backend.get_snapshot("CASE-missing")
        with self.assertRaises(ValueError):
            self.backend.seed_scenario("unknown-scenario")
        self.assertEqual(self.backend.list_cases(), [])

    def test_idempotent_lifecycle_releases_port_and_persists_records(self):
        server = self.backend._server
        address = server.server_address
        self.assertEqual(address[0], "127.0.0.1")
        self.assertGreater(address[1], 0)
        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.backend.start(), range(16)))
        self.assertIs(self.backend._server, server)
        case_id = self.backend.seed_scenario("missing_registration")
        thread = self.backend._server_thread
        self.backend.close()
        self.backend.close()
        self.assertFalse(thread.is_alive())
        with socket.socket() as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind(address)
        result = self.tool(case_id, "inspect_workflow")
        self.assertEqual(result["error"]["code"], "BACKEND_NOT_STARTED")
        self.assertEqual(self.backend.get_audit(case_id)[-1]["data"]["result"], result)
        self.backend.start()
        self.assertEqual(self.ok_tool(case_id, "inspect_workflow")["case_id"], case_id)

    def test_invalid_demo_content_is_rejected_before_database_creation(self):
        content = copy.deepcopy(CONTRACT_CONTENT)
        content["scenarios"][3]["registration_exists"] = True
        invalid_path = str(pathlib.Path(self.temp.name) / "must-not-exist.db")
        with self.assertRaises(ValueError):
            backend.SMBackend(invalid_path, content)
        self.assertFalse(pathlib.Path(invalid_path).exists())
        with self.assertRaises(ValueError):
            backend.SMBackend(":memory:", CONTRACT_CONTENT)


class ImportAndInterfaceTests(unittest.TestCase):
    def test_import_safe_in_clean_process_without_project_or_optional_dependencies(self):
        source_path = str(pathlib.Path(backend.__file__).resolve())
        script = """
import builtins, importlib.util, pathlib, socket, sqlite3, sys, threading
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'streamlit','dotenv','google','agent','ui','demo_content'}:
        raise AssertionError('Forbidden import: ' + name)
    return original_import(name, *args, **kwargs)
def forbidden(*args, **kwargs):
    raise AssertionError('Import caused database creation or server startup')
builtins.__import__ = guarded_import
sqlite3.connect = forbidden
def guard_audit(event, args):
    if event.startswith('socket.'):
        raise AssertionError('Import accessed a socket: ' + event)
sys.addaudithook(guard_audit)
threading.Thread.start = forbidden
spec = importlib.util.spec_from_file_location('candidate_backend', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert not list(pathlib.Path.cwd().iterdir()), 'Import created files'
"""
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-B", "-c", script, source_path],
                                    cwd=temp, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_exact_public_interface_and_prefixed_global_namespace(self):
        expected = {
            "__init__": ["self", "db_path", "demo_content"],
            "start": ["self"], "close": ["self"],
            "seed_scenario": ["self", "scenario_id"], "list_cases": ["self"],
            "get_snapshot": ["self", "case_id"], "get_audit": ["self", "case_id"],
            "call_tool": ["self", "case_id", "name", "args"],
            "record_agent_event": ["self", "case_id", "event"], "verify_token": ["self", "token"],
        }
        for name, parameters in expected.items():
            self.assertEqual(list(inspect.signature(getattr(backend.SMBackend, name)).parameters), parameters)
        self.assertEqual({name for name in vars(backend.SMBackend) if not name.startswith("_")}, set(expected) - {"__init__"})
        for name in vars(backend):
            if not name.startswith("__"):
                self.assertTrue(name == "SMBackend" or name.startswith(("sm_backend_", "SM_BACKEND_")), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

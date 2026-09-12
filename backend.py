import contextlib as sm_backend_contextlib
import copy as sm_backend_copy
import datetime as sm_backend_datetime
import http.client as sm_backend_http_client
import http.server as sm_backend_http_server
import json as sm_backend_json
import pathlib as sm_backend_pathlib
import re as sm_backend_re
import secrets as sm_backend_secrets
import sqlite3 as sm_backend_sqlite3
import threading as sm_backend_threading
import uuid as sm_backend_uuid


SM_BACKEND_HTTP_TIMEOUT = 10.0
SM_BACKEND_SOCKET_TIMEOUT = 5.0
SM_BACKEND_BUSY_TIMEOUT_MS = 3000
SM_BACKEND_MAX_REQUEST_BYTES = 16384
SM_BACKEND_MAX_RESPONSE_BYTES = 262144
SM_BACKEND_SCOPE = "Local demo database and inbox"
SM_BACKEND_TOOLS = frozenset({
    "inspect_workflow", "ensure_registration", "ensure_ticket",
    "deliver_ticket", "verify_completion", "escalate",
})
SM_BACKEND_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','NEEDS_REVIEW')),
    lose_ticket_response_once INTEGER NOT NULL CHECK(lose_ticket_response_once IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY NOT NULL,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id),
    status TEXT NOT NULL CHECK(status IN ('confirmed','pending')),
    UNIQUE(payment_id, case_id)
);
CREATE TABLE IF NOT EXISTS registrations (
    registration_id TEXT PRIMARY KEY NOT NULL,
    payment_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(payment_id,case_id) REFERENCES payments(payment_id,case_id),
    UNIQUE(registration_id,payment_id,case_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY NOT NULL,
    registration_id TEXT NOT NULL UNIQUE,
    payment_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL UNIQUE,
    ticket_token TEXT NOT NULL UNIQUE CHECK(length(ticket_token)=43),
    created_at TEXT NOT NULL,
    FOREIGN KEY(registration_id,payment_id,case_id)
        REFERENCES registrations(registration_id,payment_id,case_id),
    UNIQUE(ticket_id,payment_id,case_id)
);
CREATE TABLE IF NOT EXISTS inbox_deliveries (
    delivery_id TEXT PRIMARY KEY NOT NULL,
    ticket_id TEXT NOT NULL UNIQUE,
    payment_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(ticket_id,payment_id,case_id) REFERENCES tickets(ticket_id,payment_id,case_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT REFERENCES cases(case_id),
    requested_case_id TEXT,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    summary TEXT NOT NULL,
    tool TEXT,
    ok INTEGER CHECK(ok IN (0,1) OR ok IS NULL),
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_by_requested_case
    ON audit_events(requested_case_id,event_id);
CREATE TABLE IF NOT EXISTS completion_receipts (
    case_id TEXT PRIMARY KEY NOT NULL,
    payment_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    checks TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope='Local demo database and inbox'),
    FOREIGN KEY(ticket_id,payment_id,case_id) REFERENCES tickets(ticket_id,payment_id,case_id)
);
CREATE TRIGGER IF NOT EXISTS registration_requires_payment_insert
BEFORE INSERT ON registrations
WHEN NOT EXISTS (SELECT 1 FROM payments
    WHERE payment_id=NEW.payment_id AND case_id=NEW.case_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'PAYMENT_NOT_CONFIRMED'); END;
CREATE TRIGGER IF NOT EXISTS registration_requires_payment_update
BEFORE UPDATE ON registrations
WHEN NOT EXISTS (SELECT 1 FROM payments
    WHERE payment_id=NEW.payment_id AND case_id=NEW.case_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'PAYMENT_NOT_CONFIRMED'); END;
CREATE TRIGGER IF NOT EXISTS ticket_requires_payment_insert
BEFORE INSERT ON tickets
WHEN NOT EXISTS (SELECT 1 FROM payments
    WHERE payment_id=NEW.payment_id AND case_id=NEW.case_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'PAYMENT_NOT_CONFIRMED'); END;
CREATE TRIGGER IF NOT EXISTS ticket_requires_payment_update
BEFORE UPDATE ON tickets
WHEN NOT EXISTS (SELECT 1 FROM payments
    WHERE payment_id=NEW.payment_id AND case_id=NEW.case_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'PAYMENT_NOT_CONFIRMED'); END;
CREATE TRIGGER IF NOT EXISTS delivery_requires_payment_insert
BEFORE INSERT ON inbox_deliveries
WHEN NOT EXISTS (SELECT 1 FROM payments
    WHERE payment_id=NEW.payment_id AND case_id=NEW.case_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'PAYMENT_NOT_CONFIRMED'); END;
CREATE TRIGGER IF NOT EXISTS delivery_requires_payment_update
BEFORE UPDATE ON inbox_deliveries
WHEN NOT EXISTS (SELECT 1 FROM payments
    WHERE payment_id=NEW.payment_id AND case_id=NEW.case_id AND status='confirmed')
BEGIN SELECT RAISE(ABORT,'PAYMENT_NOT_CONFIRMED'); END;
COMMIT;
"""


def sm_backend_now() -> str:
    return sm_backend_datetime.datetime.now(
        sm_backend_datetime.timezone.utc
    ).isoformat(timespec="microseconds")


def sm_backend_id(prefix: str) -> str:
    return prefix + "-" + sm_backend_uuid.uuid4().hex


def sm_backend_ok(data: dict) -> dict:
    return {"ok": True, "data": data, "error": None}


def sm_backend_error(code: str, message: str, retryable: bool = False) -> dict:
    return {"ok": False, "data": {}, "error": {
        "code": code, "message": message, "retryable": retryable,
    }}


def sm_backend_json_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def sm_backend_json_constant(value: str):
    raise ValueError("Non-finite JSON numbers are not permitted.")


def sm_backend_load_json(raw: bytes):
    return sm_backend_json.loads(
        raw.decode("utf-8"), object_pairs_hook=sm_backend_json_pairs,
        parse_constant=sm_backend_json_constant,
    )


def sm_backend_valid_envelope(value) -> bool:
    if type(value) is not dict or set(value) != {"ok", "data", "error"}:
        return False
    if type(value["ok"]) is not bool or type(value["data"]) is not dict:
        return False
    error = value["error"]
    if value["ok"]:
        return error is None
    return (
        type(error) is dict and set(error) == {"code", "message", "retryable"}
        and type(error["code"]) is str and bool(error["code"])
        and type(error["message"]) is str and type(error["retryable"]) is bool
    )


def sm_backend_exception_result(exc: Exception) -> dict:
    if isinstance(exc, SM_BACKEND_ToolError):
        return sm_backend_error(exc.code, str(exc), exc.retryable)
    if isinstance(exc, sm_backend_sqlite3.Error):
        name = getattr(exc, "sqlite_errorname", type(exc).__name__)
        code = getattr(exc, "sqlite_errorcode", 0) or 0
        retryable = (code & 255) in (
            sm_backend_sqlite3.SQLITE_BUSY, sm_backend_sqlite3.SQLITE_LOCKED,
        )
        return sm_backend_error("DATABASE_ERROR", f"{name}: {exc}", retryable)
    return sm_backend_error("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


class SM_BACKEND_ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class SM_BACKEND_HTTPServer(sm_backend_http_server.ThreadingHTTPServer):
    # Join active handlers before close() returns. Each accepted socket is bounded.
    daemon_threads = False
    block_on_close = True
    request_queue_size = 64

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SM_BACKEND_SOCKET_TIMEOUT)
        return connection, address


class SM_BACKEND_HTTPHandler(sm_backend_http_server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Results go to SQLite, never to HTTP access logs or stdout.
        pass

    def _send(self, status: int, result: dict) -> None:
        raw = sm_backend_json.dumps(result, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            # The operation/result is already committed and audited. A disconnected
            # client must inspect again; its transport failure is logged by call_tool.
            return
        finally:
            self.close_connection = True

    def do_POST(self) -> None:
        backend = self.server.sm_backend_owner
        request = {}
        try:
            if self.path != "/tool":
                raise SM_BACKEND_ToolError("NOT_FOUND", "Only POST /tool is available.")
            if self.headers.get_content_type() != "application/json":
                raise SM_BACKEND_ToolError("INVALID_REQUEST", "Content-Type must be application/json.")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                raise SM_BACKEND_ToolError("INVALID_REQUEST", "One Content-Length is required.")
            if not lengths[0].isascii() or not lengths[0].isdecimal():
                raise SM_BACKEND_ToolError("INVALID_REQUEST", "Invalid Content-Length.")
            try:
                length = int(lengths[0])
            except ValueError as exc:
                raise SM_BACKEND_ToolError("INVALID_REQUEST", "Content-Length is too large.") from exc
            if not 0 < length <= SM_BACKEND_MAX_REQUEST_BYTES:
                raise SM_BACKEND_ToolError("INVALID_REQUEST", "Request body is empty or too large.")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise SM_BACKEND_ToolError("INVALID_REQUEST", "Incomplete request body.")
            try:
                request = sm_backend_load_json(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise SM_BACKEND_ToolError("INVALID_JSON", f"Invalid JSON: {exc}") from exc
            result = backend._dispatch(request)
            self._send(200, result)
        except (SM_BACKEND_ToolError, OSError) as exc:
            if isinstance(exc, OSError):
                result = sm_backend_error("REQUEST_READ_ERROR", f"{type(exc).__name__}: {exc}", True)
            else:
                result = sm_backend_exception_result(exc)
            result = backend._log_external_result(request, result, "http_rejection")
            self._send(400, result)


class SMBackend:
    def __init__(self, db_path: str, demo_content: dict):
        if type(db_path) is not str or not db_path.strip() or db_path == ":memory:":
            raise ValueError("db_path must name a persistent SQLite file, not :memory:.")
        self._scenarios = self._validate_content(demo_content)
        self._db_path = str(sm_backend_pathlib.Path(db_path).expanduser().resolve())
        self._lifecycle_lock = sm_backend_threading.RLock()
        self._server = None
        self._server_thread = None
        sm_backend_pathlib.Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sm_backend_contextlib.closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SM_BACKEND_SCHEMA)

    @staticmethod
    def _validate_content(content: dict) -> dict:
        keys = {"product_name", "tagline", "scope_note", "scenarios", "pitch_summary"}
        if type(content) is not dict or set(content) != keys:
            raise ValueError("demo_content must have exactly the Shared Contract v1 keys.")
        if content["product_name"] != "SentinelMesh" or any(
            type(content[key]) is not str for key in keys - {"scenarios"}
        ):
            raise ValueError("Invalid demo product name or presentation text.")
        if type(content["scenarios"]) is not list or not content["scenarios"]:
            raise ValueError("scenarios must be a nonempty list.")
        seed_keys = {
            "scenario_id", "title", "description", "payment_status",
            "registration_exists", "ticket_exists", "delivery_exists", "lose_ticket_response_once",
        }
        scenarios = {}
        for seed in content["scenarios"]:
            if type(seed) is not dict or set(seed) != seed_keys:
                raise ValueError("Each scenario must have exactly the contract seed keys.")
            if any(type(seed[key]) is not str for key in (
                "scenario_id", "title", "description", "payment_status",
            )) or not seed["scenario_id"] or not seed["title"]:
                raise ValueError("Scenario identifiers and presentation text must be strings.")
            if seed["scenario_id"] in scenarios:
                raise ValueError("Duplicate scenario_id.")
            if seed["payment_status"] not in {"confirmed", "pending"}:
                raise ValueError("Invalid seed payment_status.")
            flags = ("registration_exists", "ticket_exists", "delivery_exists", "lose_ticket_response_once")
            if any(type(seed[key]) is not bool for key in flags):
                raise ValueError("Seed flags must be booleans.")
            if (seed["payment_status"] == "pending" and any(seed[key] for key in flags)) or (
                seed["ticket_exists"] and not seed["registration_exists"]
            ) or (seed["delivery_exists"] and not seed["ticket_exists"]) or (
                seed["lose_ticket_response_once"] and (not seed["registration_exists"] or seed["ticket_exists"])
            ):
                raise ValueError("Seed records violate their payment or record prerequisites.")
            scenarios[seed["scenario_id"]] = sm_backend_copy.deepcopy(seed)
        return scenarios

    def _connect(self):
        connection = sm_backend_sqlite3.connect(
            self._db_path, timeout=SM_BACKEND_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        try:
            connection.row_factory = sm_backend_sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={SM_BACKEND_BUSY_TIMEOUT_MS}")
            return connection
        except Exception:
            connection.close()
            raise

    @sm_backend_contextlib.contextmanager
    def _transaction(self, write: bool = False):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._server is not None:
                return
            server = SM_BACKEND_HTTPServer(("127.0.0.1", 0), SM_BACKEND_HTTPHandler)
            server.sm_backend_owner = self
            thread = sm_backend_threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05},
                name="SentinelMesh-local-tools", daemon=True,
            )
            try:
                thread.start()
            except Exception:
                server.server_close()
                raise
            self._server, self._server_thread = server, thread

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._server is None:
                return
            self._server.shutdown()
            self._server.server_close()
            self._server_thread.join()
            self._server = None
            self._server_thread = None

    def seed_scenario(self, scenario_id: str) -> str:
        if type(scenario_id) is not str or scenario_id not in self._scenarios:
            raise ValueError("Unknown scenario_id.")
        seed = self._scenarios[scenario_id]
        case_id = sm_backend_id("CASE")
        with self._transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?)",
                (case_id, seed["title"], "OPEN", int(seed["lose_ticket_response_once"]), sm_backend_now()),
            )
            connection.execute(
                "INSERT INTO payments VALUES (?,?,?)",
                (sm_backend_id("PAY"), case_id, seed["payment_status"]),
            )
            if seed["registration_exists"]:
                self._ensure_registration(connection, case_id)
            if seed["ticket_exists"]:
                self._ensure_ticket(connection, case_id)
            if seed["delivery_exists"]:
                self._deliver_ticket(connection, case_id)
        return case_id

    def list_cases(self) -> list[dict]:
        with self._transaction() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT case_id,title,status FROM cases ORDER BY created_at DESC,case_id"
            )]

    @staticmethod
    def _case(connection, case_id: str):
        if type(case_id) is not str or not case_id:
            raise ValueError("Unknown case_id.")
        row = connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown case_id.")
        return row

    @staticmethod
    def _records(connection, case_id: str) -> tuple:
        payment = connection.execute("SELECT * FROM payments WHERE case_id=?", (case_id,)).fetchone()
        payment_id = payment["payment_id"] if payment is not None else None
        # Table names are fixed code, never taken from a request.
        collections = []
        for table in ("registrations", "tickets", "inbox_deliveries"):
            collections.append(connection.execute(
                f"SELECT * FROM {table} WHERE case_id=? OR payment_id=?",
                (case_id, payment_id),
            ).fetchall())
        return payment, *collections

    @staticmethod
    def _receipt(connection, case_id: str):
        row = connection.execute(
            "SELECT case_id,payment_id,ticket_id,verified_at,checks,scope "
            "FROM completion_receipts WHERE case_id=?", (case_id,),
        ).fetchone()
        if row is None:
            return None
        receipt = dict(row)
        receipt["checks"] = sm_backend_json.loads(receipt["checks"])
        return receipt

    def _snapshot(self, connection, case_id: str) -> dict:
        case = self._case(connection, case_id)
        payment, registrations, tickets, deliveries = self._records(connection, case_id)
        if payment is None:
            raise SM_BACKEND_ToolError("INCONSISTENT_RECORDS", "The case has no payment record.")
        return {
            "case_id": case_id, "title": case["title"], "status": case["status"],
            "payment_id": payment["payment_id"], "payment_status": payment["status"],
            "registration_id": registrations[0]["registration_id"] if registrations else None,
            "ticket_id": tickets[0]["ticket_id"] if tickets else None,
            "ticket_token": tickets[0]["ticket_token"] if tickets else None,
            "delivery_status": "delivered" if deliveries else "missing",
            "registration_count": len(registrations), "ticket_count": len(tickets),
            "delivery_count": len(deliveries), "receipt": self._receipt(connection, case_id),
        }

    def get_snapshot(self, case_id: str) -> dict:
        with self._transaction() as connection:
            return self._snapshot(connection, case_id)

    def get_audit(self, case_id: str) -> list[dict]:
        if type(case_id) is not str or not case_id:
            raise ValueError("case_id must be a nonempty string.")
        with self._transaction() as connection:
            events = []
            for row in connection.execute(
                "SELECT timestamp,type,summary,tool,ok,data FROM audit_events "
                "WHERE requested_case_id=? ORDER BY event_id", (case_id,),
            ):
                event = dict(row)
                event["ok"] = None if event["ok"] is None else bool(event["ok"])
                event["data"] = sm_backend_json.loads(event["data"])
                events.append(event)
            return events

    @staticmethod
    def _audit(connection, case_id, event_type: str, summary: str, tool, ok, data: dict) -> None:
        requested_id = case_id if type(case_id) is str else None
        known = connection.execute("SELECT case_id FROM cases WHERE case_id=?", (requested_id,)).fetchone()
        connection.execute(
            "INSERT INTO audit_events(case_id,requested_case_id,timestamp,type,summary,tool,ok,data) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (known["case_id"] if known else None, requested_id, sm_backend_now(), event_type,
             summary, tool if type(tool) is str else None, ok,
             sm_backend_json.dumps(data, allow_nan=False)),
        )

    def record_agent_event(self, case_id: str, event: dict) -> None:
        if type(event) is not dict or set(event) != {"type", "summary", "tool", "step"}:
            raise ValueError("Agent events require exactly type, summary, tool and step.")
        if (type(event["type"]) is not str or not event["type"] or
                type(event["summary"]) is not str or
                (event["tool"] is not None and type(event["tool"]) is not str) or
                type(event["step"]) is not int or event["step"] < 0):
            raise ValueError("Invalid agent event field types.")
        with self._transaction(write=True) as connection:
            self._case(connection, case_id)
            self._audit(connection, case_id, event["type"], event["summary"],
                        event["tool"], None, {"step": event["step"]})

    @staticmethod
    def _request_metadata(request) -> dict:
        if type(request) is not dict:
            return {"request_type": type(request).__name__}
        args = request.get("args")
        return {
            "case_id": request.get("case_id") if type(request.get("case_id")) is str else None,
            "name": request.get("name") if type(request.get("name")) is str else None,
            "argument_keys": sorted(str(key) for key in args) if type(args) is dict else None,
            "argument_type": type(args).__name__,
        }

    def _audit_tool(self, connection, request, result: dict, event_type: str) -> None:
        metadata = self._request_metadata(request)
        code = "OK" if result["ok"] else result["error"]["code"]
        self._audit(connection, metadata.get("case_id"), event_type,
                    f"{metadata.get('name') or 'Tool request'}: {code}",
                    metadata.get("name"), result["ok"], {"request": metadata, "result": result})

    def _log_external_result(self, request, result: dict, event_type: str) -> dict:
        try:
            with self._transaction(write=True) as connection:
                self._audit_tool(connection, request, result, event_type)
        except Exception as exc:
            # Disk/locking failures can also make an audit impossible. Report this
            # honestly without disguising the original transport/database error.
            result = sm_backend_copy.deepcopy(result)
            result["error"]["message"] += f" Audit was not persisted: {type(exc).__name__}: {exc}"
        return result

    def call_tool(self, case_id: str, name: str, args: dict) -> dict:
        request = {"case_id": case_id, "name": name, "args": args}
        try:
            raw = sm_backend_json.dumps(request, allow_nan=False).encode("utf-8")
            if len(raw) > SM_BACKEND_MAX_REQUEST_BYTES:
                raise ValueError("Request exceeds the local HTTP body limit.")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            return self._log_external_result(
                request, sm_backend_error("INVALID_REQUEST", f"Request is not valid JSON: {exc}"),
                "client_rejection",
            )
        with self._lifecycle_lock:
            address = self._server.server_address if self._server is not None else None
        if address is None:
            return self._log_external_result(request, sm_backend_error(
                "BACKEND_NOT_STARTED", "Call backend.start() before using HTTP tools.", True,
            ), "transport_error")
        connection = sm_backend_http_client.HTTPConnection(
            address[0], address[1], timeout=SM_BACKEND_HTTP_TIMEOUT,
        )
        try:
            connection.request("POST", "/tool", body=raw, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            body = response.read(SM_BACKEND_MAX_RESPONSE_BYTES + 1)
            if len(body) > SM_BACKEND_MAX_RESPONSE_BYTES:
                raise ValueError("Local HTTP response exceeded its size limit.")
            result = sm_backend_load_json(body)
            if not sm_backend_valid_envelope(result):
                raise ValueError("Local HTTP response did not match the tool envelope.")
            if response.status != 200 and result["ok"]:
                raise ValueError(f"HTTP {response.status} incorrectly claimed success.")
            # A valid error envelope is returned unchanged, including OUTCOME_UNKNOWN.
            return result
        except (OSError, sm_backend_http_client.HTTPException) as exc:
            result = sm_backend_error(
                "TRANSPORT_ERROR", f"Local HTTP {type(exc).__name__}: {exc}. "
                "The outcome may be unknown; inspect fresh records before another mutation.", True,
            )
        except (ValueError, UnicodeError, RecursionError) as exc:
            result = sm_backend_error(
                "INVALID_RESPONSE", f"Local HTTP response could not be verified: {exc}. "
                "Inspect fresh records before another mutation.", True,
            )
        finally:
            connection.close()
        return self._log_external_result(request, result, "transport_error")

    @staticmethod
    def _validate_request(connection, request) -> tuple:
        if type(request) is not dict or set(request) != {"case_id", "name", "args"}:
            raise SM_BACKEND_ToolError("INVALID_REQUEST", "Expected exactly case_id, name and args.")
        case_id, name, args = request["case_id"], request["name"], request["args"]
        if type(case_id) is not str or not case_id:
            raise SM_BACKEND_ToolError("INVALID_CASE_ID", "case_id must be a nonempty string.")
        if type(name) is not str or name not in SM_BACKEND_TOOLS:
            raise SM_BACKEND_ToolError("UNKNOWN_TOOL", "The requested tool is not allowed.")
        if type(args) is not dict:
            raise SM_BACKEND_ToolError("INVALID_ARGUMENTS", "args must be a JSON object.")
        if name == "escalate":
            if (set(args) != {"reason"} or type(args["reason"]) is not str or
                    not args["reason"].strip() or len(args["reason"]) > 1000):
                raise SM_BACKEND_ToolError("INVALID_ARGUMENTS", "escalate requires only a nonempty reason (at most 1000 characters).")
        elif args:
            raise SM_BACKEND_ToolError("INVALID_ARGUMENTS", "This tool accepts only {}.")
        if connection.execute("SELECT 1 FROM cases WHERE case_id=?", (case_id,)).fetchone() is None:
            raise SM_BACKEND_ToolError("UNKNOWN_CASE", "The requested case does not exist.")
        return case_id, name, args

    def _dispatch(self, request) -> dict:
        try:
            with self._transaction(write=True) as connection:
                connection.execute("SAVEPOINT tool_action")
                try:
                    case_id, name, args = self._validate_request(connection, request)
                    if name == "inspect_workflow":
                        result = sm_backend_ok(self._snapshot(connection, case_id))
                    elif name == "ensure_registration":
                        result = self._ensure_registration(connection, case_id)
                    elif name == "ensure_ticket":
                        result = self._ensure_ticket(connection, case_id)
                    elif name == "deliver_ticket":
                        result = self._deliver_ticket(connection, case_id)
                    elif name == "verify_completion":
                        result = self._verify_completion(connection, case_id)
                    else:
                        result = self._escalate(connection, case_id, args["reason"])
                except Exception as exc:
                    connection.execute("ROLLBACK TO tool_action")
                    result = sm_backend_exception_result(exc)
                connection.execute("RELEASE tool_action")
                self._audit_tool(connection, request, result, "tool_result")
            # Both writes and the actual result are committed before HTTP returns.
            return result
        except Exception as exc:
            return self._log_external_result(request, sm_backend_exception_result(exc), "tool_error")

    @staticmethod
    def _confirmed_payment(connection, case_id: str):
        payment = connection.execute("SELECT * FROM payments WHERE case_id=?", (case_id,)).fetchone()
        if payment is None:
            raise SM_BACKEND_ToolError("INCONSISTENT_RECORDS", "The case has no payment record.")
        if payment["status"] != "confirmed":
            raise SM_BACKEND_ToolError("PAYMENT_NOT_CONFIRMED", "Payment is pending.")
        return payment

    def _ensure_registration(self, connection, case_id: str) -> dict:
        payment = self._confirmed_payment(connection, case_id)
        row = connection.execute("SELECT * FROM registrations WHERE payment_id=?", (payment["payment_id"],)).fetchone()
        if row is not None:
            if row["case_id"] != case_id:
                raise SM_BACKEND_ToolError("INCONSISTENT_RECORDS", "Registration belongs to another case.")
            return sm_backend_ok({"registration_id": row["registration_id"], "created": False})
        registration_id = sm_backend_id("REG")
        connection.execute("INSERT INTO registrations VALUES (?,?,?,?)",
                           (registration_id, payment["payment_id"], case_id, sm_backend_now()))
        return sm_backend_ok({"registration_id": registration_id, "created": True})

    def _ensure_ticket(self, connection, case_id: str) -> dict:
        payment = self._confirmed_payment(connection, case_id)
        registration = connection.execute(
            "SELECT * FROM registrations WHERE payment_id=? AND case_id=?",
            (payment["payment_id"], case_id),
        ).fetchone()
        if registration is None:
            raise SM_BACKEND_ToolError("REGISTRATION_REQUIRED", "A matching registration is required.")
        row = connection.execute("SELECT * FROM tickets WHERE payment_id=?", (payment["payment_id"],)).fetchone()
        if row is not None:
            if (row["case_id"] != case_id or row["registration_id"] != registration["registration_id"]
                    or not self._verify_token(connection, row["ticket_token"])["valid"]):
                raise SM_BACKEND_ToolError("INCONSISTENT_RECORDS", "The existing ticket is not valid for this payment and registration.")
            return sm_backend_ok({"ticket_id": row["ticket_id"], "ticket_token": row["ticket_token"], "created": False})
        ticket_id, token = sm_backend_id("TICKET"), sm_backend_secrets.token_urlsafe(32)
        connection.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?)", (
            ticket_id, registration["registration_id"], payment["payment_id"], case_id, token, sm_backend_now(),
        ))
        if self._case(connection, case_id)["lose_ticket_response_once"]:
            connection.execute("UPDATE cases SET lose_ticket_response_once=0 WHERE case_id=?", (case_id,))
            # Return, do not raise: dispatch must commit this intentional ambiguity.
            return sm_backend_error("OUTCOME_UNKNOWN", "Simulated lost ticket response: the write may have committed. Inspect current records.", True)
        return sm_backend_ok({"ticket_id": ticket_id, "ticket_token": token, "created": True})

    def _deliver_ticket(self, connection, case_id: str) -> dict:
        payment = self._confirmed_payment(connection, case_id)
        ticket = connection.execute(
            "SELECT * FROM tickets WHERE payment_id=? AND case_id=?", (payment["payment_id"], case_id),
        ).fetchone()
        if ticket is None:
            raise SM_BACKEND_ToolError("TICKET_REQUIRED", "A matching valid ticket is required.")
        if not self._verify_token(connection, ticket["ticket_token"])["valid"]:
            raise SM_BACKEND_ToolError("INVALID_TICKET", "The ticket does not resolve to a matching confirmed payment and registration.")
        delivery = connection.execute("SELECT * FROM inbox_deliveries WHERE payment_id=?", (payment["payment_id"],)).fetchone()
        if delivery is not None:
            if delivery["ticket_id"] != ticket["ticket_id"] or delivery["case_id"] != case_id:
                raise SM_BACKEND_ToolError("INCONSISTENT_RECORDS", "Delivery references a different ticket or case.")
            return sm_backend_ok({"delivery_id": delivery["delivery_id"], "created": False})
        delivery_id = sm_backend_id("DELIVERY")
        connection.execute("INSERT INTO inbox_deliveries VALUES (?,?,?,?,?)", (
            delivery_id, ticket["ticket_id"], payment["payment_id"], case_id, sm_backend_now(),
        ))
        return sm_backend_ok({"delivery_id": delivery_id, "created": True})

    @staticmethod
    def _verify_token(connection, token: str) -> dict:
        invalid = {"valid": False, "case_id": None, "ticket_id": None}
        if type(token) is not str or not sm_backend_re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return invalid
        rows = connection.execute("""
            SELECT t.case_id,t.ticket_id FROM tickets AS t
            JOIN registrations AS r ON r.registration_id=t.registration_id
                AND r.payment_id=t.payment_id AND r.case_id=t.case_id
            JOIN payments AS p ON p.payment_id=t.payment_id AND p.case_id=t.case_id
            JOIN cases AS c ON c.case_id=t.case_id
            WHERE t.ticket_token=? AND p.status='confirmed'
        """, (token,)).fetchall()
        if len(rows) != 1:
            return invalid
        return {"valid": True, "case_id": rows[0]["case_id"], "ticket_id": rows[0]["ticket_id"]}

    def verify_token(self, token: str) -> dict:
        with self._transaction() as connection:
            return self._verify_token(connection, token)

    def _completion_checks(self, connection, case_id: str) -> tuple:
        # Query source tables in one transaction; never use a snapshot or prior receipt.
        payment, registrations, tickets, deliveries = self._records(connection, case_id)
        payment_id = payment["payment_id"] if payment else None
        registration = registrations[0] if len(registrations) == 1 else None
        ticket = tickets[0] if len(tickets) == 1 else None
        delivery = deliveries[0] if len(deliveries) == 1 else None
        registration_ok = registration is not None and payment is not None and (
            registration["payment_id"] == payment_id and registration["case_id"] == case_id
        )
        ticket_ok = ticket is not None and registration_ok and (
            ticket["registration_id"] == registration["registration_id"]
            and ticket["payment_id"] == payment_id and ticket["case_id"] == case_id
        )
        token_result = self._verify_token(connection, ticket["ticket_token"]) if ticket else {
            "valid": False, "case_id": None, "ticket_id": None,
        }
        token_ok = bool(ticket_ok and token_result == {
            "valid": True, "case_id": case_id, "ticket_id": ticket["ticket_id"],
        })
        delivery_ok = delivery is not None and ticket_ok and (
            delivery["ticket_id"] == ticket["ticket_id"]
            and delivery["payment_id"] == payment_id and delivery["case_id"] == case_id
        )
        checks = [
            {"name": "payment_confirmed", "passed": bool(payment and payment["status"] == "confirmed"),
             "evidence": f"Payment {payment_id}: {payment['status'] if payment else 'missing'}."},
            {"name": "one_matching_registration", "passed": bool(registration_ok),
             "evidence": f"Registration count: {len(registrations)}; payment and case match: {bool(registration_ok)}."},
            {"name": "one_matching_ticket", "passed": bool(ticket_ok),
             "evidence": f"Ticket count: {len(tickets)}; registration, payment and case match: {bool(ticket_ok)}."},
            {"name": "ticket_token_valid", "passed": token_ok,
             "evidence": f"Stored token resolves to the matching valid ticket: {token_ok}."},
            {"name": "one_matching_inbox_delivery", "passed": bool(delivery_ok),
             "evidence": f"Local inbox count: {len(deliveries)}; ticket, payment and case match: {bool(delivery_ok)}."},
        ]
        return checks, payment_id, ticket["ticket_id"] if ticket else None

    def _verify_completion(self, connection, case_id: str) -> dict:
        checks, payment_id, ticket_id = self._completion_checks(connection, case_id)
        if not all(check["passed"] for check in checks):
            # If records changed outside this API, a historical receipt is not current success.
            connection.execute("UPDATE cases SET status='OPEN' WHERE case_id=? AND status='RESOLVED'", (case_id,))
            return sm_backend_ok({"complete": False, "checks": checks, "receipt": None})
        receipt = self._receipt(connection, case_id)
        if receipt is None:
            receipt = {"case_id": case_id, "payment_id": payment_id, "ticket_id": ticket_id,
                       "verified_at": sm_backend_now(), "checks": checks, "scope": SM_BACKEND_SCOPE}
            connection.execute("INSERT INTO completion_receipts VALUES (?,?,?,?,?,?)", (
                case_id, payment_id, ticket_id, receipt["verified_at"],
                sm_backend_json.dumps(checks, allow_nan=False), SM_BACKEND_SCOPE,
            ))
        connection.execute("UPDATE cases SET status='RESOLVED' WHERE case_id=?", (case_id,))
        return sm_backend_ok({"complete": True, "checks": checks, "receipt": receipt})

    def _escalate(self, connection, case_id: str, reason: str) -> dict:
        checks, _, _ = self._completion_checks(connection, case_id)
        if not all(check["passed"] for check in checks):
            connection.execute("UPDATE cases SET status='NEEDS_REVIEW' WHERE case_id=?", (case_id,))
        # A complete OPEN case still needs verify_completion to issue a receipt.
        return sm_backend_ok({"status": self._case(connection, case_id)["status"], "reason": reason})

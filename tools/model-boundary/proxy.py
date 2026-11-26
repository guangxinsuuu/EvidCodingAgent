"""受信模型出口：产品 Gateway → 固定本地 Ollama → 私有原件账本。

只接受登记模型的 chat/completions；无下载、任意 URL、模型选择或 shell 能力。
预算与操作身份先在 SQLite 事务登记，再发送一次。UNKNOWN 保留额度和请求，
同一 Job 只能读取旧结果。独立观察 token 可读原件，产品 token 无原件读取权限。
本机 Docker/宿主管理员仍是信任根，此出口不声称是企业级网络沙盒。
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("NONFINITE_JSON")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class Denied(ValueError):
    def __init__(self, code, status=403):
        self.code, self.status = code, status
        super().__init__(code)


class LocalProvider:
    """只有固定宿主 Ollama；测试以注入对象替换，配置与请求都不能改成任意 URL。"""
    def request(self, method, path, raw=None, *, timeout=100, limit=131072):
        if (method, path) not in {("GET", "/api/tags"), ("POST", "/v1/chat/completions")}:
            raise Denied("PROVIDER_ROUTE_DENIED")
        conn = http.client.HTTPConnection("host.docker.internal", 11434, timeout=timeout)
        def abort():
            if conn.sock:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            conn.close()
        deadline = threading.Timer(timeout, abort)
        deadline.daemon = True
        deadline.start()
        try:
            conn.request(method, path, raw, {"Content-Type": "application/json", "Accept": "application/json"})
            response = conn.getresponse()
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("PROVIDER_RESPONSE_TOO_LARGE")
            return response.status, body
        finally:
            deadline.cancel()
            conn.close()


class Outlet:
    """Handler 调用；配置固定候选部署、权重、额度，原件只落本实例专有卷。"""
    def __init__(self, config, database, *, provider=None):
        self.config, self.database = config, database
        self.provider = provider or LocalProvider()
        if not re.fullmatch(r"evid-sut-[a-f0-9]{12}", config["deployment_id"]) \
                or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", config["model_name"]) \
                or not re.fullmatch(r"[a-f0-9]{64}", config["model_digest"]) \
                or type(config["max_calls"]) is not int or not 1 <= config["max_calls"] <= 12 \
                or type(config["max_output_tokens"]) is not int or not 128 <= config["max_output_tokens"] <= 2048 \
                or any(not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", config[k]) for k in ("product_token", "observer_token")) \
                or hmac.compare_digest(config["product_token"], config["observer_token"]):
            raise ValueError("MODEL_OUTLET_CONFIG_INVALID")
        self.expires_at = datetime.fromisoformat(config["expires_at"])
        if self.expires_at.tzinfo is None:
            raise ValueError("MODEL_LEASE_TIMEZONE_REQUIRED")
        identity = {k:config[k] for k in ("deployment_id", "model_name", "model_digest", "max_calls", "max_output_tokens", "expires_at")}
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(operation_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                  request_text TEXT NOT NULL, state TEXT NOT NULL, response_text TEXT, upstream_status INTEGER,
                  response_sha256 TEXT, admitted_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER,
                  model_before TEXT, model_after TEXT, error_code TEXT);
            """)
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT body FROM identity WHERE singleton=1").fetchone()
            if previous and parse(previous[0]) != identity:
                raise ValueError("MODEL_OUTLET_IDENTITY_CHANGED")
            conn.execute("INSERT OR IGNORE INTO identity VALUES(1,?)", (encode(identity).decode(),))

    def connect(self):
        conn = sqlite3.connect(self.database, timeout=3)
        conn.row_factory = sqlite3.Row
        return conn

    def recover(self):
        """独占进程锁获得后调用；上进程未取得完整回执的请求只改 UNKNOWN，绝不补发。"""
        with self.connect() as conn:
            conn.execute("UPDATE calls SET state='UNKNOWN',finished_at=?,error_code='OUTLET_RESTARTED' WHERE state='DISPATCHED'", (now(),))

    def role(self, authorization):
        for role, key in (("PRODUCT", "product_token"), ("OBSERVER", "observer_token")):
            if hmac.compare_digest(authorization.encode(), ("Bearer " + self.config[key]).encode()):
                return role
        raise Denied("MODEL_IDENTITY_DENIED", 401)

    def live(self):
        if datetime.now(timezone.utc) >= self.expires_at:
            raise Denied("MODEL_LEASE_EXPIRED", 409)

    def model_identity(self):
        """每次新推理前后核对实际 tags；它是本地观察，非供应商签名或权重远程证明。"""
        status, raw = self.provider.request("GET", "/api/tags", timeout=2, limit=65536)
        value = parse(raw)
        rows = [r for r in value.get("models", []) if r.get("name") == self.config["model_name"]]
        if status != 200 or len(rows) != 1 or rows[0].get("digest") != self.config["model_digest"]:
            raise Denied("MODEL_MANIFEST_DRIFT", 503)
        return {"name": rows[0]["name"], "digest": rows[0]["digest"], "observed_at": now()}

    def validate_request(self, operation_id, raw):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise Denied("MODEL_OPERATION_REQUIRED", 422)
        if len(raw) > 65536:
            raise Denied("MODEL_INPUT_TOO_LARGE", 413)
        value = parse(raw)
        if type(value) is not dict or set(value) != {"model", "stream", "temperature", "max_tokens", "response_format", "messages"} \
                or value["model"] != self.config["model_name"] or value["stream"] is not False \
                or type(value["temperature"]) is not int or value["temperature"] != 0 \
                or type(value["max_tokens"]) is not int or not 1 <= value["max_tokens"] <= self.config["max_output_tokens"] \
                or type(value["response_format"]) is not dict or value["response_format"].get("type") != "json_schema" \
                or not isinstance(value["response_format"].get("json_schema"), dict):
            raise Denied("MODEL_REQUEST_OUTSIDE_SCOPE", 422)
        messages = value["messages"]
        if not isinstance(messages, list) or len(messages) != 2 or [m.get("role") if isinstance(m, dict) else None for m in messages] != ["system", "user"] \
                or any(set(m) != {"role", "content"} or not isinstance(m["content"], str) or not m["content"] for m in messages):
            raise Denied("MODEL_MESSAGES_INVALID", 422)
        return value

    @staticmethod
    def replay(row, request_hash):
        if row["request_sha256"] != request_hash:
            raise Denied("MODEL_OPERATION_CONFLICT", 409)
        if row["state"] == "COMPLETE":
            return 200, row["response_text"].encode()
        if row["state"] == "REJECTED":
            return row["upstream_status"], row["response_text"].encode()
        # 包括 DISPATCHED：并发同 key 查询不能再发一次推理，也不能冒充同步已完成。
        raise Denied("MODEL_RESULT_UNCONFIRMED_NO_RESEND", 503)

    def invoke(self, operation_id, raw):
        self.live()
        self.validate_request(operation_id, raw)
        request_hash = digest(raw)
        with self.connect() as conn:
            old = conn.execute("SELECT * FROM calls WHERE operation_id=?", (operation_id,)).fetchone()
        if old:
            return self.replay(old, request_hash)
        before = self.model_identity()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM calls WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                return self.replay(old, request_hash)
            if conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= self.config["max_calls"]:
                raise Denied("MODEL_BUDGET_EXHAUSTED", 429)
            if conn.execute("SELECT COUNT(*) FROM calls WHERE state='DISPATCHED'").fetchone()[0]:
                raise Denied("MODEL_CONCURRENCY_LIMIT", 429)
            conn.execute("INSERT INTO calls(operation_id,request_sha256,request_text,state,admitted_at,model_before) VALUES(?,?,?,'DISPATCHED',?,?)",
                         (operation_id, request_hash, raw.decode(), now(), encode(before).decode()))
        started = time.monotonic()
        status, response, after, state, error = None, None, None, "UNKNOWN", None
        try:
            status, response = self.provider.request("POST", "/v1/chat/completions", raw)
            # 完整拒绝保留其正文与成本空缺；5xx 可能出现在推理以后，仍为 UNKNOWN。
            if 400 <= status < 500:
                state = "REJECTED"
            elif status == 200:
                body = parse(response)
                after = self.model_identity()
                if not isinstance(body, dict) or body.get("model") != self.config["model_name"]:
                    raise ValueError("MODEL_RESPONSE_IDENTITY_MISMATCH")
                state = "COMPLETE"
            else:
                error = "PROVIDER_RESULT_UNCONFIRMED"
        except Exception as exc:
            error = exc.code if isinstance(exc, Denied) else type(exc).__name__
        finally:
            with self.connect() as conn:
                conn.execute("UPDATE calls SET state=?,response_text=?,upstream_status=?,response_sha256=?,finished_at=?,elapsed_ms=?,model_after=?,error_code=? WHERE operation_id=?",
                    (state, response.decode(errors="replace") if response is not None else None, status,
                     digest(response) if response is not None else None, now(), round((time.monotonic()-started)*1000),
                     encode(after).decode() if after else None, error, operation_id))
        if state not in {"COMPLETE", "REJECTED"}:
            raise Denied("MODEL_RESULT_UNCONFIRMED_NO_RESEND", 503)
        return status, response

    def observation(self, operation_id):
        """仅 Handler 的 OBSERVER 路由可调用；允许过期后读取原件用于有界对账。"""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM calls WHERE operation_id=?", (operation_id,)).fetchone()
            used = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        if not row:
            raise Denied("MODEL_OPERATION_NOT_OBSERVED", 404)
        record = dict(row)
        return {"schema_version": 1, "deployment_id": self.config["deployment_id"], "operation": record,
                "model_name": self.config["model_name"], "model_digest": self.config["model_digest"],
                "budget": {"reserved_calls": used, "max_calls": self.config["max_calls"],
                           "billing": "LOCAL_MODEL_NO_PROVIDER_INVOICE", "currency_cost": None},
                "observed_at": now(), "authority": "LOCAL_MODEL_OUTLET_NOT_BUSINESS_ORACLE"}


def handler(outlet):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, writing):
            self.connection.settimeout(5)
            try:
                headers = self.headers.get_all("Authorization", [])
                if len(headers) != 1:
                    raise Denied("MODEL_IDENTITY_DENIED", 401)
                role = outlet.role(headers[0])
                if not writing and self.path == "/v1/models":
                    outlet.live()
                    observed = outlet.model_identity()
                    value = {"object": "list", "data": [{"id": observed["name"], "object": "model"}],
                             "model_digest": observed["digest"], "deployment_id": outlet.config["deployment_id"]}
                    status, raw = 200, encode(value)
                elif not writing and re.fullmatch(r"/v1/operations/[a-f0-9]{32}", self.path) and role == "OBSERVER":
                    status, raw = 200, encode(outlet.observation(self.path.rsplit("/", 1)[1]))
                elif writing and self.path == "/v1/chat/completions" and role == "PRODUCT":
                    lengths = self.headers.get_all("Content-Length", [])
                    operations = self.headers.get_all("X-EGAP-Model-Operation", [])
                    if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit() \
                            or not 1 <= int(lengths[0]) <= 65536 or len(operations) != 1 \
                            or self.headers.get_content_type() != "application/json":
                        raise Denied("MODEL_HTTP_ENVELOPE_INVALID", 422)
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        raise Denied("MODEL_BODY_INCOMPLETE", 422)
                    status, raw = outlet.invoke(operations[0], raw)
                else:
                    raise Denied("MODEL_ROUTE_DENIED")
            except Denied as exc:
                status, raw = exc.status, encode({"error_code": exc.code})
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                status, raw = 422, encode({"error_code": "MODEL_INPUT_INVALID"})
            except Exception:
                status, raw = 503, encode({"error_code": "MODEL_OUTLET_UNAVAILABLE"})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass  # 回执已持久；客户端离线不允许再次执行模型。
    return Handler


if __name__ == "__main__":
    os.umask(0o077)
    with Path("/data/process.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outlet = Outlet(parse(Path("/run/secrets/role.json").read_bytes()), Path("/data/calls.sqlite"))
        outlet.recover()
        ThreadingHTTPServer(("0.0.0.0", 8080), handler(outlet)).serve_forever()

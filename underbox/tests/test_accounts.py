"""Gateway regressions for the current SQLite/shared-worker architecture.

Migrates the old JSON/per-user-process checks without using real credentials.
Report/history storage is also tested against the real handler in test_current_accounts.
"""
import concurrent.futures
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store
from accounts import Accounts, Workers, account_handler
from serve import Handler


class SyntheticWorker(BaseHTTPRequestHandler):
    """Echo routing decisions so credentials/cookies and account mixups are visible."""
    def log_message(self, *args):
        pass

    def do_GET(self):
        owner = self.headers.get("X-Underbox-User")
        data = json.dumps({"owner": owner, "report": owner,
                           "cookieForwarded": bool(self.headers.get("Cookie")),
                           "nameForwarded": bool(self.headers.get("X-Underbox-Name"))}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        data = b"".join(("data: " + json.dumps({"owner": self.headers.get("X-Underbox-User"),
                                              "step": i}) + "\n\n").encode() for i in range(2))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.accounts = Accounts(cls.root / "accounts.db")
        cls.env_file = cls.root / "synthetic.env"
        cls.env_file.write_text("SEARCH_PROVIDER=zhipu\nSEARCH_API_KEY=synthetic-search\n"
                                "DEEPSEEK_API_KEY=synthetic-llm\n", encoding="utf-8")
        with store.connect(cls.accounts.path) as conn:
            for uid in ("a", "b", "admin", "empty"):
                store.create_user(conn, uid, "合成用户" + uid, "pass-" + uid,
                                  role="admin" if uid == "admin" else "user",
                                  quota_total=0 if uid in ("admin", "empty") else 10,
                                  env_file=str(cls.env_file))
            conn.commit()
        cls.worker = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticWorker)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                        account_handler(Handler, cls.accounts, cls.worker.server_port))
        cls.port = cls.server.server_port
        for server in (cls.worker, cls.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.worker):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.accounts.attempts.clear()

    def request(self, method, path, data=None, cookie="", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        hs = {"Content-Type": "application/json", "Cookie": cookie}
        hs.update(headers or {})
        try:
            conn.request(method, path, json.dumps(data) if data is not None else None, hs)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login(self, uid, username=None):
        status, headers, raw = self.request("POST", "/api/login",
                                             {"username": username or uid, "password": "pass-" + uid})
        self.assertEqual(status, 200)
        user = json.loads(raw)["user"]
        self.assertEqual(user["id"], uid)
        self.assertEqual(user["name"], "合成用户" + uid)
        self.assertNotIn("password_hash", user)
        self.assertNotIn("env_file", user)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_invalid_password_and_missing_session_block_api(self):
        self.assertEqual(self.request("POST", "/api/login", {"username": "a", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": "pass-a"})[0], 401)
        for path in ("/api/report", "/api/export", "/api/plan", "/api/history"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("POST", "/api/split/stream", {})[0], 401)

    def test_case_insensitive_login_and_session_public_fields(self):
        cookie = self.login("a", username=" A ")
        status, _, raw = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 200)
        session = json.loads(raw)
        self.assertTrue(session["authed"])
        self.assertEqual(session["user"]["name"], "合成用户a")
        self.assertNotIn("password_hash", session["user"])

    def test_concurrent_users_route_with_correct_identity_without_cookies(self):
        cookies = {u: self.login(u) for u in ("a", "b")}
        def fetch(uid):
            status, _, raw = self.request("GET", "/api/report", cookie=cookies[uid])
            self.assertEqual(status, 200)
            payload = json.loads(raw)
            self.assertEqual(payload["report"], uid)
            self.assertFalse(payload["cookieForwarded"])
            self.assertFalse(payload["nameForwarded"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, ["a", "b"] * 5))

    def test_sse_streams_keep_correct_account(self):
        for uid in ("a", "b"):
            status, headers, raw = self.request("POST", "/api/verify/stream", {}, self.login(uid))
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/event-stream")
            chunks = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
            self.assertEqual([x["owner"] for x in chunks], [uid, uid])

    def test_logout_revokes_server_session(self):
        cookie = self.login("a")
        status, headers, _ = self.request("POST", "/api/logout", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/report", cookie=cookie)[0], 401)

    def test_expired_session_is_rejected(self):
        cookie = self.login("a")
        with store.connect(self.accounts.path) as conn:
            conn.execute("UPDATE sessions SET expires_at='2000-01-01 00:00:00' WHERE token=?",
                         (cookie.split("=", 1)[1],))
            conn.commit()
        self.assertEqual(self.request("GET", "/api/report", cookie=cookie)[0], 401)

    def test_disabled_user_cannot_log_in_or_keep_a_session(self):
        cookie = self.login("b")
        try:
            with store.connect(self.accounts.path) as conn:
                store.set_enabled(conn, "b", False)
                conn.commit()
            self.assertEqual(self.request("GET", "/api/report", cookie=cookie)[0], 401)
            self.assertEqual(self.request("POST", "/api/login", {"username": "b", "password": "pass-b"})[0], 401)
        finally:
            with store.connect(self.accounts.path) as conn:
                store.set_enabled(conn, "b", True)
                conn.commit()

    def test_account_switch_rejects_requests_from_old_tab(self):
        cookie = self.login("b")
        status, _, raw = self.request("POST", "/api/verify/stream", {}, cookie, {"X-Underbox-User": "a"})
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(raw)["accountChanged"])

    def test_source_and_secret_files_are_not_downloadable(self):
        for path in ("/serve.py", "/accounts.py", "/.env", "/../users/accounts.json", "/index.html.bak"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_cross_origin_login_and_logout_are_rejected(self):
        bad_origin = {"Origin": "https://elsewhere.invalid"}
        self.assertEqual(self.request("POST", "/api/login", {"username": "a", "password": "pass-a"},
                                     headers=bad_origin)[0], 403)
        cookie = self.login("a")
        self.assertEqual(self.request("POST", "/api/logout", cookie=cookie, headers=bad_origin)[0], 403)
        self.assertTrue(json.loads(self.request("GET", "/api/session", cookie=cookie)[2])["authed"])

    def test_environment_uses_configured_keys_instead_of_ambient_credentials(self):
        workers = Workers(self.accounts, Path(__file__).resolve().parents[1] / "serve.py")
        with patch.dict(os.environ, {"SEARCH_API_KEY": "wrong", "BRAVE_SEARCH_API_KEY": "wrong",
                                     "DEEPSEEK_API_KEY": "wrong", "UNDERBOX_GATEWAY_DB": "wrong",
                                     "UNDERBOX_USER_ID": "wrong", "UNDERBOX_ACCOUNTS_FILE": "wrong"}):
            env = workers.environment()
        self.assertEqual(env["SEARCH_API_KEY"], "synthetic-search")
        self.assertEqual(env["DEEPSEEK_API_KEY"], "synthetic-llm")
        for key in ("BRAVE_SEARCH_API_KEY", "UNDERBOX_ACCOUNTS_FILE", "UNDERBOX_GATEWAY_DB", "UNDERBOX_USER_ID"):
            self.assertNotIn(key, env)
        self.assertEqual(env["UNDERBOX_PASSWORD"], "")
        self.assertEqual(env["UNDERBOX_DB"], str(self.accounts.path))

    def test_admin_search_key_override_takes_priority_over_env(self):
        workers = Workers(self.accounts, Path(__file__).resolve().parents[1] / "serve.py")
        try:
            with store.connect(self.accounts.path) as conn:
                store.set_settings(conn, {"zhipu_api_key": "synthetic-admin"})
                conn.commit()
            self.assertEqual(workers.environment()["SEARCH_API_KEY"], "synthetic-admin")
            with store.connect(self.accounts.path) as conn:
                store.set_settings(conn, {"search_api_key": "synthetic-explicit"})
                conn.commit()
            self.assertEqual(workers.environment()["SEARCH_API_KEY"], "synthetic-explicit")
        finally:
            with store.connect(self.accounts.path) as conn:
                store.set_settings(conn, {"zhipu_api_key": "", "search_api_key": ""})
                conn.commit()

    def test_http_login_attempts_are_limited(self):
        data = {"username": "a", "password": "wrong"}
        for _ in range(10):
            self.assertEqual(self.request("POST", "/api/login", data)[0], 401)
        self.assertEqual(self.request("POST", "/api/login", data)[0], 429)

    def test_workers_refuse_ports_with_an_active_listener(self):
        workers = Workers(self.accounts, Path(__file__).resolve().parents[1] / "serve.py",
                          port=self.worker.server_port)
        with self.assertRaises(OSError):
            workers.start()

    def test_admin_permissions_and_zero_quota_gate(self):
        cookie = self.login("a")
        self.assertEqual(self.request("GET", "/api/admin/users", cookie=cookie)[0], 403)
        status, _, raw = self.request("GET", "/api/admin/users", cookie=self.login("admin"))
        self.assertEqual(status, 200)
        self.assertEqual({u["id"] for u in json.loads(raw)["users"]}, {"a", "b", "admin", "empty"})
        self.assertNotIn(b"password_hash", raw)
        self.assertNotIn(b"synthetic-search", raw)
        status, _, raw = self.request("POST", "/api/verify/stream", {}, self.login("empty"))
        self.assertEqual(status, 402)
        self.assertTrue(json.loads(raw)["quotaExhausted"])
        self.assertEqual(self.request("POST", "/api/verify/stream", {}, self.login("admin"))[0], 200)


if __name__ == "__main__":
    unittest.main()

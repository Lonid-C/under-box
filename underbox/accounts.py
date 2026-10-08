"""口令登录网关；每位用户的流水线、密钥、报告、用量运行在独立进程。

UNDERBOX_ACCOUNTS_FILE 指定只读用户配置，密钥放在静态目录外的独立 .env。
网关不运行核验，不在请求间改 os.environ。线程池沿用各自进程的环境。
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

COOKIE = "ub_account"
SESSION_SECONDS = 7 * 86400
MAX_UPLOAD = 20 * 1024 * 1024


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2-sha256${salt}${digest}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        kind, salt, _ = encoded.split("$")
        return kind == "pbkdf2-sha256" and hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    password_hash: str
    env_file: Path
    port: int

    def public(self) -> dict:
        return {"id": self.id, "name": self.name}


class Accounts:
    def __init__(self, path: str | Path):
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        self.users = []
        for item in data["users"]:
            env_file = Path(item["env_file"])
            if not env_file.is_absolute():
                env_file = path.parent / env_file
            user = Account(item["id"], item["name"], item["password_hash"],
                           env_file.resolve(), int(item["port"]))
            if not user.env_file.is_file() or not 1024 <= user.port <= 65535:
                raise ValueError(f"用户配置不可用：{user.id}")
            if any(u.id == user.id or u.port == user.port for u in self.users):
                raise ValueError("用户标识与工作端口必须唯一")
            self.users.append(user)
        if not self.users:
            raise ValueError("必须配置至少一个用户")
        self.sessions: dict[str, tuple[Account, float]] = {}
        self.attempts: dict[str, deque] = {}
        self.lock = threading.Lock()

    def authenticate(self, password: str) -> Account | None:
        for user in self.users:
            if password_matches(password, user.password_hash):
                return user
        return None

    def session(self, token: str) -> Account | None:
        with self.lock:
            item = self.sessions.get(token)
            if not item:
                return None
            user, expires = item
            if expires <= time.time():
                self.sessions.pop(token, None)
                return None
            return user

    def login(self, user: Account, previous: str = "") -> str:
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
            self.sessions.pop(previous, None)
            if len(self.sessions) >= 10000:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (user, now + SESSION_SECONDS)
            return token

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def allow_attempt(self, ip: str) -> bool:
        with self.lock:
            now = time.monotonic()
            self.attempts = {k: v for k, v in self.attempts.items() if v and v[-1] > now - 60}
            attempts = self.attempts.setdefault(ip, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return False
            attempts.append(now)
            return True


class Workers:
    def __init__(self, accounts: Accounts, script: Path):
        self.accounts, self.script = accounts, script
        self.processes: dict[str, subprocess.Popen] = {}
        self.stopping = threading.Event()

    def environment(self, user: Account) -> dict:
        # 不继承网关中 app.__init__ 可能加载的默认密钥和业务参数。
        # 每个用户完整读取自己的 env，空缺字段沿用程序默认值。
        from app.env import parse_env_text
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "HOME", "LANG", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}
               or k.startswith("LC_")}
        env.update(parse_env_text(user.env_file.read_text()))
        env.update(UNDERBOX_ENV_FILE=str(user.env_file), UNDERBOX_PASSWORD="",
                   UNDERBOX_USER_ID=user.id, UNDERBOX_USER_NAME=user.name,
                   OPEN_BOX_ROOT=str(self.script.parent.parent / "open_box"),
                   PYTHONUNBUFFERED="1", MODE="live")
        # 用户 env 不能将工作进程再次变成网关。
        env.pop("UNDERBOX_ACCOUNTS_FILE", None)
        if not (env.get("SEARCH_API_KEY") or env.get("BRAVE_SEARCH_API_KEY")):
            raise ValueError(f"用户缺少搜索配置：{user.id}")
        if not (env.get("DEEPSEEK_API_KEY") or env.get("LLM_API_KEY") or env.get("ZHIPU_API_KEY")):
            raise ValueError(f"用户缺少模型配置：{user.id}")
        return env

    def launch(self, user: Account) -> None:
        self.processes[user.id] = subprocess.Popen(
            [sys.executable, str(self.script), "--host", "127.0.0.1", "--port", str(user.port)],
            cwd=self.script.parent, env=self.environment(user))

    def start(self) -> None:
        for user in self.accounts.users:
            # 端口被占用时失败，不能代理到不属于该用户的旧进程。
            with socket.socket() as probe:
                # 与 HTTPServer 一样允许复用已关闭连接的 TIME_WAIT 端口。
                # 活跃的监听进程仍会使 bind 失败，不能放宽用户进程隔离。
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", user.port))
            self.environment(user)
        for user in self.accounts.users:
            self.launch(user)
        deadline = time.monotonic() + 20
        for user in self.accounts.users:
            while True:
                if self.processes[user.id].poll() is not None:
                    raise RuntimeError(f"用户工作进程启动失败：{user.id}")
                try:
                    with socket.create_connection(("127.0.0.1", user.port), timeout=.3):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("用户工作进程启动超时")
                    self.stopping.wait(.1)
        threading.Thread(target=self.supervise, daemon=True).start()

    def supervise(self) -> None:
        while not self.stopping.wait(3):
            for user in self.accounts.users:
                if self.processes[user.id].poll() is not None:
                    print(f"重新启动用户工作进程：{user.id}", flush=True)
                    self.launch(user)

    def stop(self) -> None:
        self.stopping.set()
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self.processes.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def account_handler(base, accounts: Accounts):
    class Gateway(base):
        def user(self) -> Account | None:
            return accounts.session(self._cookie(COOKIE))

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.close_connection = True
                self._json({"error": "请从本站登录页面操作"}, 403)
                return False
            return True

        def _cookie_header(self, value: str, age: int) -> dict:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return {"Set-Cookie": f"{COOKIE}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"}

        def _body(self, limit: int = MAX_UPLOAD) -> bytes | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= limit or self.headers.get("Transfer-Encoding"):
                    raise ValueError
            except ValueError:
                self.close_connection = True
                self._json({"error": "请求过大或格式不正确"}, 413)
                return None
            return self.rfile.read(n)

        def _login(self) -> None:
            if not self._same_origin():
                return
            raw = self._body(4096)
            if raw is None:
                return
            ip = self.client_address[0]
            if ip == "127.0.0.1":
                ip = self.headers.get("X-Real-IP") or ip
            if not accounts.allow_attempt(ip):
                return self._json({"error": "尝试次数过多，请一分钟后重试"}, 429)
            try:
                payload = json.loads(raw)
                password = str(payload.get("password") or "").strip()
            except (ValueError, AttributeError):
                password = ""
            user = accounts.authenticate(password) if password and len(password) <= 256 else None
            if user is None:
                return self._json({"ok": False, "error": "口令不正确"}, 401)
            token = accounts.login(user, self._cookie(COOKIE))
            return self._json({"ok": True, "gate": True, "user": user.public()},
                              headers=self._cookie_header(token, SESSION_SECONDS))

        def _logout(self) -> None:
            if not self._same_origin():
                return
            if self._body(4096) is None:
                return
            accounts.logout(self._cookie(COOKIE))
            return self._json({"ok": True}, headers=self._cookie_header("", 0))

        def _forward(self, user: Account) -> None:
            expected = self.headers.get("X-Underbox-User")
            if expected and expected != user.id:
                self.close_connection = True
                return self._json({"error": "当前账户已切换，请重新载入页面",
                                   "accountChanged": True, "loginRequired": True}, 409)
            body = self._body() if self.command == "POST" else None
            if self.command == "POST" and body is None:
                return
            headers = {k: self.headers[k] for k in ("Content-Type", "X-Filename") if k in self.headers}
            conn = http.client.HTTPConnection("127.0.0.1", user.port, timeout=1800)
            sent = False
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() in {"content-type", "content-length", "content-disposition"}:
                        self.send_header(key, value)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                sent = True
                while True:
                    chunk = response.read1(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (OSError, http.client.HTTPException):
                if not sent:
                    self._json({"error": "当前用户的服务暂时不可用，请稍后重试"}, 502)
            finally:
                conn.close()

        def _asset(self, name: str) -> None:
            # Python 源码、配置文件、备份与隐藏文件不能由静态路由下载。
            if any(p.startswith(".") for p in Path(name).parts) or Path(name).suffix.lower() not in {
                ".html", ".css", ".js", ".pdf", ".png", ".jpg", ".svg", ".ico", ".woff2"}:
                return self._send(b"not found", "text/plain", 404)
            return super()._asset(name)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            user = self.user()
            if path == "/api/session":
                return self._json({"gate": True, "authed": user is not None,
                                   "user": user.public() if user else None})
            if path == "/api/health" and user is None:
                return self._json({"ok": True, "loginRequired": True})
            if path.startswith("/api/"):
                if user is None:
                    return self._json({"error": "请先输入你的登录口令", "loginRequired": True}, 401)
                return self._forward(user)
            return self._asset("index.html" if path == "/" else path.lstrip("/"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/login":
                return self._login()
            if path == "/api/logout":
                return self._logout()
            if not self._same_origin():
                return
            user = self.user()
            if user is None:
                self.close_connection = True
                return self._json({"error": "请先输入你的登录口令", "loginRequired": True}, 401)
            if path.startswith("/api/"):
                return self._forward(user)
            self.close_connection = True
            return self._json({"error": "not found"}, 404)

    return Gateway


def serve_accounts(base, config: str, host: str, port: int) -> int:
    accounts = Accounts(config)
    script = Path(__file__).with_name("serve.py")
    workers = Workers(accounts, script)
    server = None
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        workers.start()
        server = ThreadingHTTPServer((host, port), account_handler(base, accounts))
        print(f"underbox 用户登录网关 http://{host}:{port}/，用户数 {len(accounts.users)}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        workers.stop()
    return 0

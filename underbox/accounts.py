"""账户网关：用户名 + 密码登录、角色权限、API 配额与审批。

架构（沿用原有设计，只把"配置"换成"数据库"）：

    浏览器 ──HTTPS/HTTP──> 网关(本文件) ──转发──> 每个用户一个独立工作进程
                             │                      (serve.py，各自端口/env)
                             └── SQLite(store.py): 账号·会话·配额·用量·申请·访问

为什么仍然"每用户一个工作进程"：核验是分钟级的长任务，共用一个进程会让 A 的长核验
把 B 卡住；而且报告存在进程内存里，共用会互相覆盖。隔离的代价是内存（本机 13 个
进程约 600MB，服务器 3.7G 够用）。

配额口径：一次**核验**（/api/verify/stream）算一次配额；拆分与问答只记用量不扣配额。
  · 网关在转发核验请求**前**检查余额，不足直接 402 并提示申请；
  · 由工作进程在核验**真正完成后**记账并扣减（见 serve.py），
    这样"启动了但立刻失败"的请求不会白扣一次。
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import store  # 同目录

COOKIE = "ub_account"
MAX_UPLOAD = 20 * 1024 * 1024
VERIFY_PATHS = ("/api/verify/stream",)          # 扣配额的路由
ACCESS_SKIP = (".css", ".js", ".png", ".jpg", ".svg", ".ico", ".woff2", ".pdf")


class Accounts:
    """账号与会话。真正的存储都在 SQLite，这里只做业务判断。"""

    def __init__(self, db: str | Path | None = None):
        self.path = store.init(db)
        self.attempts: dict[str, deque] = {}
        self.lock = threading.Lock()

    # -- 登录 ---------------------------------------------------------------
    def authenticate(self, username: str, password: str):
        """用户名 + 密码。用户名大小写不敏感（输错大小写很常见）。"""
        with store.connect(self.path) as conn:
            row = conn.execute("SELECT * FROM users WHERE lower(id) = lower(?)",
                               (username.strip(),)).fetchone()
            if row is None or not row["enabled"]:
                return None
            if not store.password_matches(password, row["password_hash"]):
                return None
            return dict(row)

    def login(self, user_id: str, *, ip: str = "", ua: str = "") -> str:
        with store.connect(self.path) as conn:
            token = store.create_session(conn, user_id, ip=ip, ua=ua)
            store.touch_login(conn, user_id)
            conn.commit()
        return token

    def session(self, token: str):
        if not token:
            return None
        with store.connect(self.path) as conn:
            row = store.session_user(conn, token)
            return dict(row) if row else None

    def logout(self, token: str) -> None:
        with store.connect(self.path) as conn:
            store.delete_session(conn, token)
            conn.commit()

    # -- 登录限速（内存即可，重启清零无妨）----------------------------------
    def allow_attempt(self, ip: str) -> bool:
        with self.lock:
            now = time.monotonic()
            self.attempts = {k: v for k, v in self.attempts.items() if v and v[-1] > now - 60}
            q = self.attempts.setdefault(ip, deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= 10:
                return False
            q.append(now)
            return True


class Workers:
    """所有用户**共享一个**工作进程；挂了自动拉起。

    原设计是"每用户一个进程"（密钥隔离 + 故障隔离）。但实测在 3.7G 内存的机器上，
    13 个解释器会被 OOM killer 连锅端掉——systemd 再拉起，又 OOM，load average
    冲到 500+，连 SSH 都连不上。而这些账号的密钥本来就来自同一份 env 文件，
    真正要防的是"报告与画像串台"，那在 serve.py 里按请求头 X-Underbox-User
    分存就够了，不需要多进程。

    代价：一个人跑长核验时其他人要排队（原来是进程间并行）。13 人的内部工具，
    这笔取舍划算——先保证机器活着。
    """

    def __init__(self, accounts: Accounts, script: Path, port: int = 8788):
        self.accounts, self.script, self.port = accounts, script, port
        self.process: subprocess.Popen | None = None
        self.stopping = threading.Event()

    def _env_file(self) -> Path:
        with store.connect(self.accounts.path) as conn:
            row = conn.execute(
                "SELECT env_file FROM users WHERE enabled = 1 AND env_file IS NOT NULL "
                "ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise ValueError("没有任何启用中的用户，拿不到可用的密钥文件")
        return Path(row["env_file"])

    def environment(self) -> dict:
        # 不继承网关里可能加载的默认密钥；整份读用户的 env 文件。
        from app.env import parse_env_text
        env_file = self._env_file()
        env = {k: v for k, v in os.environ.items()
               if k in {"PATH", "HOME", "LANG", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"}
               or k.startswith("LC_")}
        env.update(parse_env_text(env_file.read_text()))
        # 管理台里改过的配置优先于 env 文件（映射表在 store.SETTING_ENVS）
        with store.connect(self.accounts.path) as conn:
            env.update(store.settings_env(conn))
        env.update(
            UNDERBOX_ENV_FILE=str(env_file),
            UNDERBOX_PASSWORD="",                    # 单实例口令门禁已由本网关取代
            # ⚠ 这里**绝对不能**设 UNDERBOX_GATEWAY_DB —— serve.py 用那个变量判断
            # "我自己是不是网关"，设了就递归启动下一层网关（曾经因此 13 个用户
            # 变成 26+ 个进程，把 3.7G 内存撑爆、被 OOM killer 连锅端）。
            # 工作进程只需要这两样：库在哪、以及"该记账"这个标记。
            UNDERBOX_DB=str(self.accounts.path),
            UNDERBOX_GATEWAY_WORKER="1",
            OPEN_BOX_ROOT=str(self.script.parent.parent / "open_box"),
            PYTHONUNBUFFERED="1", MODE="live")
        env.pop("UNDERBOX_ACCOUNTS_FILE", None)
        # 共享进程不预设身份：每个请求由网关带上 X-Underbox-User，
        # serve.py 据此分存报告/画像，也据此记账。
        env.pop("UNDERBOX_USER_ID", None)
        env.pop("UNDERBOX_USER_NAME", None)
        if not (env.get("SEARCH_API_KEY") or env.get("BRAVE_SEARCH_API_KEY")):
            raise ValueError("缺少搜索配置")
        if not (env.get("DEEPSEEK_API_KEY") or env.get("LLM_API_KEY") or env.get("ZHIPU_API_KEY")):
            raise ValueError("缺少模型配置")
        return env

    def launch(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(self.script), "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.script.parent, env=self.environment())

    def start(self) -> None:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.port))
        self.environment()                       # 缺密钥就在这里失败，别等进程起来
        self.launch()
        deadline = time.monotonic() + 60
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("工作进程启动失败")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.3):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("工作进程启动超时")
                self.stopping.wait(.1)
        threading.Thread(target=self.supervise, daemon=True).start()

    def supervise(self) -> None:
        while not self.stopping.wait(3):
            if self.process and self.process.poll() is not None:
                print("重新启动工作进程", flush=True)
                self.launch()

    def reload(self) -> None:
        """重启工作进程让新配置生效。期间核验不可用（几秒），调用方要如实告知。"""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.launch()
        deadline = time.monotonic() + 60
        while True:
            if self.process.poll() is not None:
                raise RuntimeError("工作进程重启失败（配置可能有问题，看服务日志）")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.3):
                    return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("工作进程重启超时")
                self.stopping.wait(.1)

    def stop(self) -> None:
        self.stopping.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


# ── API 连通性自检 ────────────────────────────────────────────────────────
# 在网关进程里直接发最小请求。不经过工作进程，也不写入任何状态——
# 这样"还没保存"的配置也能先测一把再决定要不要用。
LLM_PRESETS = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash",
                 ("deepseek_api_key", "llm_api_key")),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.7-flash",
              ("zhipu_api_key", "llm_api_key", "search_api_key")),
    "openai-compatible": ("", "", ("llm_api_key",)),
}
ZHIPU_SEARCH_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"
MASK_CHAR = "•"

# 管理台下拉里的候选项。都是"建议值"而不是白名单——模型名和引擎名随时会变，
# 前端用 datalist 呈现，也允许直接手填。
SETTING_OPTIONS = {
    "llm_provider": ["deepseek", "zhipu", "openai-compatible"],
    "llm_model": ["deepseek-flash", "deepseek-v4-pro", "glm-4.7-flash"],
    "llm_thinking": ["disabled", "enabled"],
    "search_provider": ["zhipu", "brave", "generic"],
    "search_engine": ["search_std", "search_pro"],
    "search_engine_open": ["search_pro_sogou", "search_pro_quark", "search_pro"],
}


def _pick(cfg: dict, names: tuple) -> str:
    for name in names:
        if cfg.get(name):
            return cfg[name]
    return ""


def probe_llm(cfg: dict) -> dict:
    import httpx
    provider = (cfg.get("llm_provider") or "deepseek").lower()
    preset = LLM_PRESETS.get(provider)
    if preset is None:
        return {"ok": False, "detail": f"未知的模型供应商：{provider}"}
    endpoint = (cfg.get("llm_endpoint") or preset[0]).strip()
    model = (cfg.get("llm_model") or preset[1]).strip()
    key = _pick(cfg, preset[2])
    if not endpoint or not model:
        return {"ok": False, "detail": "端点和模型名都要填（自定义供应商没有预设可依）"}
    if not key:
        return {"ok": False, "detail": f"缺少密钥：{' 或 '.join(preset[2])} 至少要有一个"}
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                            json={"model": model, "temperature": 0, "max_tokens": 8,
                                  "messages": [{"role": "user", "content": "ping"}]})
        if r.status_code == 200:
            return {"ok": True, "detail": f"{provider} · {model} 已连通"}
        hint = {401: "密钥无效", 402: "余额不足", 404: "端点或模型名不对",
                429: "被限流"}.get(r.status_code, "")
        return {"ok": False, "detail": f"HTTP {r.status_code} {hint}｜{r.text[:160]}"}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "detail": f"连不上：{type(exc).__name__}: {exc}"}


def probe_search(cfg: dict) -> dict:
    import httpx
    provider = (cfg.get("search_provider") or "zhipu").lower()
    try:
        with httpx.Client(timeout=20) as client:
            if provider == "brave":
                key = cfg.get("brave_api_key") or ""
                if not key:
                    return {"ok": False, "detail": "使用 Brave 就必须填 brave_api_key"}
                r = client.get("https://api.search.brave.com/res/v1/web/search",
                               params={"q": "test", "count": 2},
                               headers={"X-Subscription-Token": key, "Accept": "application/json"})
                if r.status_code == 200:
                    return {"ok": True, "detail": "Brave 已连通"}
                return {"ok": False, "detail": f"HTTP {r.status_code}｜{r.text[:160]}"}
            # 默认智谱
            engine = cfg.get("search_engine") or "search_std"
            endpoint = (cfg.get("search_endpoint") or ZHIPU_SEARCH_ENDPOINT).strip()
            key = cfg.get("search_api_key") or cfg.get("zhipu_api_key") or ""
            if not key:
                return {"ok": False, "detail": "缺少密钥：search_api_key（智谱搜索与 GLM 共用一把）"}
            r = client.post(endpoint, headers={"Authorization": f"Bearer {key}"},
                            json={"search_engine": engine, "search_query": "测试"})
            if r.status_code != 200:
                return {"ok": False, "detail": f"HTTP {r.status_code}｜{r.text[:160]}"}
            hits = (r.json() or {}).get("search_result") or []
            return {"ok": True, "detail": f"智谱 {engine} 已连通，返回 {len(hits)} 条"}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "detail": f"连不上：{type(exc).__name__}: {exc}"}


def account_handler(base, accounts: Accounts, worker_port: int, workers: "Workers" = None):
    class Gateway(base):
        # -- 身份 ----------------------------------------------------------
        def user(self) -> dict | None:
            return accounts.session(self._cookie(COOKIE))

        def _is_admin(self, user: dict | None) -> bool:
            return bool(user and user.get("role") == "admin")

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.close_connection = True
                self._json({"error": "请从本站登录页面操作"}, 403)
                return False
            return True

        def _cookie_header(self, value: str, age: int) -> dict:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return {"Set-Cookie": f"{COOKIE}={value}; Path=/; Max-Age={age}; "
                                  f"HttpOnly; SameSite=Strict{secure}"}

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

        def _payload(self, limit: int = 65536) -> dict:
            raw = self._body(limit)
            if raw is None:
                return {}
            try:
                data = json.loads(raw or b"{}")
                return data if isinstance(data, dict) else {}
            except (ValueError, AttributeError):
                return {}

        def _client_ip(self) -> str:
            ip = self.client_address[0]
            if ip == "127.0.0.1":
                ip = self.headers.get("X-Real-IP") or ip
            return ip

        def _public(self, user: dict) -> dict:
            """交给前端的账号信息。不含密码哈希，只带该角色该看的字段。"""
            total, used = int(user.get("quota_total") or 0), int(user.get("quota_used") or 0)
            return {
                "id": user["id"], "name": user["name"], "role": user.get("role", "user"),
                "quota": {"total": total, "used": used,
                          "left": -1 if total < 0 else max(0, total - used),
                          "unlimited": total < 0},
            }

        def _log_access(self, user_id: str | None, status: int) -> None:
            path = self.path.split("?", 1)[0]
            if path.endswith(ACCESS_SKIP):
                return
            try:
                with store.connect(accounts.path) as conn:
                    store.record_access(conn, user_id=user_id, ip=self._client_ip(),
                                        method=self.command, path=path, status=status)
                    conn.commit()
            except Exception:
                pass                     # 统计绝不能影响正常请求

        # -- 登录 / 登出 ----------------------------------------------------
        def _login(self) -> None:
            if not self._same_origin():
                return
            data = self._payload(4096)
            ip = self._client_ip()
            if not accounts.allow_attempt(ip):
                self._log_access(None, 429)
                return self._json({"error": "尝试次数过多，请一分钟后重试"}, 429)
            username = str(data.get("username") or "").strip()
            password = str(data.get("password") or "")
            if not username or not password or len(password) > 256 or len(username) > 64:
                self._log_access(None, 401)
                return self._json({"ok": False, "error": "请输入用户名与密码"}, 401)
            user = accounts.authenticate(username, password)
            if user is None:
                self._log_access(None, 401)
                return self._json({"ok": False, "error": "用户名或密码不正确"}, 401)
            token = accounts.login(user["id"], ip=ip, ua=self.headers.get("User-Agent", ""))
            self._log_access(user["id"], 200)
            return self._json({"ok": True, "gate": True, "user": self._public(user)},
                              headers=self._cookie_header(token, 7 * 86400))

        def _logout(self) -> None:
            if not self._same_origin():
                return
            if self._body(4096) is None:
                return
            accounts.logout(self._cookie(COOKIE))
            return self._json({"ok": True}, headers=self._cookie_header("", 0))

        # -- 用户自己的配额与申请 -------------------------------------------
        def _quota(self, user: dict) -> None:
            with store.connect(accounts.path) as conn:
                stats = store.user_usage(conn, user["id"], days=30)
                mine = [dict(r) for r in conn.execute(
                    "SELECT id, amount, reason, status, created_at, decided_at, note "
                    "FROM quota_requests WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
                    (user["id"],))]
            return self._json({"user": self._public(user), "usage": stats, "requests": mine})

        def _quota_request(self, user: dict) -> None:
            data = self._payload(8192)
            try:
                amount = int(data.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            if not 1 <= amount <= 1000:
                return self._json({"error": "申请次数需在 1–1000 之间"}, 400)
            reason = str(data.get("reason") or "").strip()
            with store.connect(accounts.path) as conn:
                req = store.create_request(conn, user["id"], amount, reason)
                conn.commit()
            return self._json({"ok": True, "id": req, "message": "申请已提交，等待管理员审批"})

        # -- API 配置（管理台可改）--------------------------------------------
        def _env_fallback(self) -> dict:
            """工作进程实际还会读的那份 env 文件——管理台没配过的项由它兜底。

            连通性测试必须带上它，否则会出现"明明能用、却报缺少密钥"这种假故障。
            """
            try:
                from app.env import parse_env_text          # noqa: PLC0415
                with store.connect(accounts.path) as conn:
                    row = conn.execute(
                        "SELECT env_file FROM users WHERE enabled = 1 AND env_file IS NOT NULL "
                        "ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    return {}
                return parse_env_text(Path(row["env_file"]).read_text())
            except Exception:                                # noqa: BLE001
                return {}

        def _merged_settings(self, incoming: dict) -> dict:
            """把"本次提交的改动"叠到"实际生效的配置"上。

            三层顺序：库里的值 → env 文件兜底 → 本次提交的改动。
            密钥在页面上显示为掩码（••••1234）：原样回传说明"没动过"，
            这时必须保留真值，否则会把掩码本身当成密钥存进去。
            """
            with store.connect(accounts.path) as conn:
                merged = dict(store.get_settings(conn))
            raw_env = self._env_fallback()
            for key, env_name in store.SETTING_ENVS.items():
                if not merged.get(key) and raw_env.get(env_name):
                    merged[key] = raw_env[env_name]
            for key, value in incoming.items():
                if key not in store.SETTING_ENVS:
                    continue
                value = "" if value is None else str(value).strip()
                # 这里是"测试"用的合并，语义是"我填了什么就用什么"：
                # 空值表示"这个框我没填"，要保留现有值（含 env 文件兜底），
                # 绝不能拿空串去覆盖——否则"不填 key 只点测试"会假报缺少密钥。
                # （保存路径的语义不同：那里空值 = 显式清掉该项。）
                if not value or (key in store.SECRET_KEYS and value.startswith(MASK_CHAR)):
                    continue
                merged[key] = value
            return merged

        def _save_settings(self) -> None:
            # 掩码（••••1234）是"没改过"的意思，绝不能把它当值存进去——
            # 页面原样回传掩码是常态（表单里的密钥框就显示着它）。
            fields = {}
            for key, value in self._payload().items():
                if key not in store.SETTING_ENVS:
                    continue
                text = "" if value is None else str(value).strip()
                if key in store.SECRET_KEYS and text.startswith(MASK_CHAR):
                    continue
                fields[key] = text
            if not fields:
                # 提交过来的全是掩码（密钥框原样回传）说明"什么也没改"，不是错误
                with store.connect(accounts.path) as conn:
                    snapshot = store.settings_public(conn)
                return self._json({"ok": True, "reloaded": False,
                                   "note": "没有需要保存的改动", "settings": snapshot})
            with store.connect(accounts.path) as conn:
                store.set_settings(conn, fields)
                conn.commit()
                snapshot = store.settings_public(conn)
            # 配置要重启工作进程才生效——如实把"重启了没有"告诉前端
            reloaded, note = False, ""
            if workers is not None:
                try:
                    workers.reload()
                    reloaded = True
                except Exception as exc:                 # noqa: BLE001
                    note = f"配置已保存，但工作进程重启失败：{exc}"
            return self._json({"ok": True, "reloaded": reloaded, "note": note,
                               "settings": snapshot})

        def _test_settings(self) -> None:
            payload = self._payload()
            cfg = self._merged_settings(payload)
            target = str(payload.get("target") or "all")
            result = {}
            if target in ("all", "llm"):
                result["llm"] = probe_llm(cfg)
            if target in ("all", "search"):
                result["search"] = probe_search(cfg)
            return self._json({"ok": all(v.get("ok") for v in result.values()), "result": result})

        # -- 管理员 ---------------------------------------------------------
        def _admin(self, user: dict) -> None:
            """所有 /api/admin/* 的唯一入口。先验角色，再分发。"""
            path = self.path.split("?", 1)[0]
            # 配置类接口自己管连接：保存后要重启工作进程，不能把连接一直捏在手里
            if path == "/api/admin/settings" and self.command == "GET":
                with store.connect(accounts.path) as conn:
                    return self._json({"settings": store.settings_public(conn),
                                       "options": SETTING_OPTIONS})
            if path == "/api/admin/settings" and self.command == "POST":
                return self._save_settings()
            if path == "/api/admin/settings/test" and self.command == "POST":
                return self._test_settings()
            with store.connect(accounts.path) as conn:
                if path == "/api/admin/overview" and self.command == "GET":
                    fresh = store.get_user(conn, user["id"])
                    data = {
                        "access": store.access_summary(conn, days=14),
                        "usage": store.usage_daily(conn, days=14),
                        "users": store.usage_by_user(conn, days=30),
                        "pending": store.pending_count(conn),
                        "requests": store.list_requests(conn, "pending"),
                    }
                    return self._json({**data, "me": self._public(dict(fresh or user))})

                if path == "/api/admin/users" and self.command == "GET":
                    return self._json({"users": [
                        {**self._public(dict(r)),
                         "enabled": bool(r["enabled"]),
                         "last_login_at": r["last_login_at"]}
                        for r in store.list_users(conn)]})

                if path == "/api/admin/requests" and self.command == "GET":
                    return self._json({"requests": store.list_requests(conn, None, 200)})

                if self.command != "POST":
                    return self._json({"error": "not found"}, 404)
                data = self._payload()

                if path == "/api/admin/quota":
                    target = str(data.get("userId") or "")
                    if store.get_user(conn, target) is None:
                        return self._json({"error": "账号不存在"}, 404)
                    try:
                        total = int(data.get("total"))
                    except (TypeError, ValueError):
                        return self._json({"error": "配额必须是整数（-1 表示不限）"}, 400)
                    store.set_quota(conn, target, total)
                    conn.commit()
                    return self._json({"ok": True, "user": self._public(dict(store.get_user(conn, target)))})

                if path == "/api/admin/decide":
                    try:
                        rid = int(data.get("id"))
                    except (TypeError, ValueError):
                        return self._json({"error": "缺少申请编号"}, 400)
                    ok = store.decide_request(conn, rid, approve=bool(data.get("approve")),
                                              admin_id=user["id"], note=str(data.get("note") or ""))
                    conn.commit()
                    if not ok:
                        return self._json({"error": "该申请已被处理过"}, 409)
                    return self._json({"ok": True, "pending": store.pending_count(conn)})

                if path == "/api/admin/user":
                    target = str(data.get("userId") or "")
                    row = store.get_user(conn, target)
                    if row is None:
                        return self._json({"error": "账号不存在"}, 404)
                    if target == user["id"] and ("enabled" in data and not data["enabled"]
                                                 or "password" in data):
                        return self._json({"error": "不能停用或改自己的密码，请让另一位管理员操作"}, 400)
                    if "enabled" in data:
                        store.set_enabled(conn, target, bool(data["enabled"]))
                        if not data["enabled"]:
                            conn.execute("DELETE FROM sessions WHERE user_id = ?", (target,))
                    if data.get("password"):
                        if len(str(data["password"])) < 8:
                            return self._json({"error": "新密码至少 8 位"}, 400)
                        store.set_password(conn, target, str(data["password"]))
                    if data.get("role") in ("admin", "user"):
                        if target == user["id"] and data["role"] != "admin":
                            return self._json({"error": "不能取消自己的管理员身份"}, 400)
                        conn.execute("UPDATE users SET role = ? WHERE id = ?", (data["role"], target))
                    conn.commit()
                    return self._json({"ok": True})

                return self._json({"error": "not found"}, 404)

        # -- 转发到该用户的工作进程 -----------------------------------------
        def _quota_left(self, user: dict) -> bool:
            if user.get("role") == "admin":
                return True
            total = int(user.get("quota_total") or 0)
            return total < 0 or int(user.get("quota_used") or 0) < total

        def _forward(self, user: dict) -> None:
            expected = self.headers.get("X-Underbox-User")
            if expected and expected != user["id"]:
                self.close_connection = True
                return self._json({"error": "当前账户已切换，请重新载入页面",
                                   "accountChanged": True, "loginRequired": True}, 409)
            path = self.path.split("?", 1)[0]
            # 配额闸门放在这里：核验开始前就拦住，不让用户跑完才发现超额
            if self.command == "POST" and path in VERIFY_PATHS and not self._quota_left(user):
                return self._json({"error": "本周期配额已用完，可提交申请由管理员审批",
                                   "quotaExhausted": True, "user": self._public(user)}, 402)
            body = self._body() if self.command == "POST" else None
            if self.command == "POST" and body is None:
                return
            headers = {k: self.headers[k] for k in ("Content-Type", "X-Filename") if k in self.headers}
            # 关键：告诉工作进程"这次是谁在用"。共享进程靠这个头把报告、画像、
            # 台账按用户分存，记账时也靠它认人（不能再依赖进程自己的环境变量）。
            headers["X-Underbox-User"] = user["id"]
            headers["X-Underbox-Name"] = user["name"]
            conn = http.client.HTTPConnection("127.0.0.1", int(worker_port), timeout=1800)
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

        # -- 静态资源 -------------------------------------------------------
        def _asset(self, name: str) -> None:
            # Python 源码、配置文件、备份与隐藏文件不能由静态路由下载。
            if any(p.startswith(".") for p in Path(name).parts) or Path(name).suffix.lower() not in {
                    ".html", ".css", ".js", ".pdf", ".png", ".jpg", ".svg", ".ico", ".woff2"}:
                return self._send(b"not found", "text/plain", 404)
            return super()._asset(name)

        # -- 路由 -----------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            user = self.user()
            if path == "/api/session":
                if user is None:
                    return self._json({"gate": True, "authed": False, "user": None})
                with store.connect(accounts.path) as conn:      # 每次取新的配额数字
                    fresh = store.get_user(conn, user["id"])
                return self._json({"gate": True, "authed": True,
                                   "user": self._public(dict(fresh or user))})
            if path == "/api/health" and user is None:
                return self._json({"ok": True, "loginRequired": True})
            if path.startswith("/api/admin/"):
                if user is None:
                    return self._json({"error": "请先登录", "loginRequired": True}, 401)
                if not self._is_admin(user):
                    self._log_access(user["id"], 403)
                    return self._json({"error": "仅管理员可访问"}, 403)
                self._log_access(user["id"], 200)
                return self._admin(user)
            if path.startswith("/api/"):
                if user is None:
                    return self._json({"error": "请先登录", "loginRequired": True}, 401)
                if path == "/api/quota":
                    self._log_access(user["id"], 200)
                    return self._quota(user)
                self._log_access(user["id"], 200)
                return self._forward(user)
            self._log_access(user["id"] if user else None, 200)
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
                return self._json({"error": "请先登录", "loginRequired": True}, 401)
            if path.startswith("/api/admin/"):
                if not self._is_admin(user):
                    self._log_access(user["id"], 403)
                    return self._json({"error": "仅管理员可访问"}, 403)
                return self._admin(user)
            if path == "/api/quota/request":
                self._log_access(user["id"], 200)
                return self._quota_request(user)
            if path.startswith("/api/"):
                self._log_access(user["id"], 200)
                return self._forward(user)
            self.close_connection = True
            return self._json({"error": "not found"}, 404)

    return Gateway


def serve_accounts(base, db: str, host: str, port: int) -> int:
    accounts = Accounts(db)
    script = Path(__file__).with_name("serve.py")
    workers = Workers(accounts, script)
    server = None

    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        workers.start()
        server = ThreadingHTTPServer((host, port),
                                     account_handler(base, accounts, workers.port, workers))
        with store.connect(accounts.path) as conn:
            n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        print(f"underbox 账户网关 http://{host}:{port}/，账号 {n} 个，"
              f"共享工作进程 127.0.0.1:{workers.port}，库 {accounts.path}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        workers.stop()
    return 0

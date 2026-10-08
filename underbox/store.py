"""账户与用量的持久层（SQLite）。

为什么是 SQLite：服务器上没有装任何数据库服务，而 Python 自带 sqlite3。
13 个账号、单机、低并发——这正好是 SQLite 的舒适区：零依赖、单文件便于备份、
WAL 模式下允许"网关进程读 + 各用户工作进程写"同时进行。

两类进程共用这个库，靠环境变量 `UNDERBOX_USER_ID` 区分身份：
  · 网关（accounts.py）—— 建会话、校验配额、审批、出看板；
  · 工作进程（serve.py）—— 核验结束后追加一条 usage_log 并消耗一次配额。

时间一律存**服务器本地时间**（服务器是 CST），看板按天聚合就不用再换算时区。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION_SECONDS = 7 * 86400

# ── 表结构 ────────────────────────────────────────────────────────────────
# 迁移策略：每次启动跑一遍 CREATE TABLE IF NOT EXISTS（幂等），
# 加字段时在 _migrate() 里补 ALTER TABLE，用 meta.schema 记录版本。
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 账号：id 是登录名。quota_total = -1 表示不限额（admin 与不限量账号）
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',      -- admin | user
  password_hash TEXT NOT NULL,                     -- pbkdf2-sha256$salt$digest
  env_file      TEXT,                              -- 该用户的密钥文件（网关用它起工作进程）
  port          INTEGER,                           -- 该用户工作进程端口
  quota_total   INTEGER NOT NULL DEFAULT 0,        -- 配额（次核验）
  quota_used    INTEGER NOT NULL DEFAULT 0,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  ip         TEXT,
  ua         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);

-- 一次核验/问答/拆分记一条；看板的金额与 token 都从这里聚合
CREATE TABLE IF NOT EXISTS usage_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      TEXT NOT NULL,
  at           TEXT NOT NULL,
  kind         TEXT NOT NULL,                      -- verify | split | ask
  claims       INTEGER NOT NULL DEFAULT 0,
  seconds      REAL    NOT NULL DEFAULT 0,
  ds_calls     INTEGER NOT NULL DEFAULT 0,
  ds_in        INTEGER NOT NULL DEFAULT 0,
  ds_cached    INTEGER NOT NULL DEFAULT 0,
  ds_out       INTEGER NOT NULL DEFAULT 0,
  glm_searches INTEGER NOT NULL DEFAULT 0,
  yuan         REAL,                               -- 单价没配时为 NULL，不编造金额
  ok           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_usage_user_at ON usage_log(user_id, at);
CREATE INDEX IF NOT EXISTS idx_usage_at      ON usage_log(at);

CREATE TABLE IF NOT EXISTS quota_requests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT NOT NULL,
  amount     INTEGER NOT NULL,                     -- 申请新增多少次
  reason     TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | rejected
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_req_status ON quota_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_req_user   ON quota_requests(user_id, created_at);

-- 访问流水：看板的"访问量"来自这里。只记路径与状态，不记正文
CREATE TABLE IF NOT EXISTS access_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  at      TEXT NOT NULL,
  user_id TEXT,
  ip      TEXT,
  method  TEXT NOT NULL,
  path    TEXT NOT NULL,
  status  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_access_at   ON access_log(at);
CREATE INDEX IF NOT EXISTS idx_access_user ON access_log(user_id, at);
"""

SCHEMA_VERSION = "1"


def now() -> str:
    """服务器本地时间（秒精度）。看板按天聚合不需要再换算时区。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_path(path: str | Path | None = None) -> Path:
    """库文件位置：UNDERBOX_DB 优先，默认放在 users/ 下（与 accounts.json 同目录）。"""
    if path:
        return Path(path)
    env = os.environ.get("UNDERBOX_DB")
    if env:
        return Path(env)
    return HERE.parent / "users" / "underbox.db"


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL：网关与各用户工作进程同时读写；busy_timeout 让并发写自动重试而不是立刻报错
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | Path | None = None) -> Path:
    p = db_path(path)
    with connect(p) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (SCHEMA_VERSION,))
        conn.commit()
    try:
        os.chmod(p, 0o600)                 # 里面有密码哈希，别让他人读
    except OSError:
        pass
    return p


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补字段。当前 schema 已是 v1，留好入口给以后加列。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col, ddl in (("role", "TEXT NOT NULL DEFAULT 'user'"),
                     ("quota_total", "INTEGER NOT NULL DEFAULT 0"),
                     ("quota_used", "INTEGER NOT NULL DEFAULT 0"),
                     ("enabled", "INTEGER NOT NULL DEFAULT 1"),
                     ("last_login_at", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")


# ── 口令 ──────────────────────────────────────────────────────────────────
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2-sha256${salt}${digest}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        kind, salt, _ = encoded.split("$")
        return kind == "pbkdf2-sha256" and hmac.compare_digest(hash_password(password, salt), encoded)
    except (ValueError, TypeError):
        return False


# ── 账号 ──────────────────────────────────────────────────────────────────
def create_user(conn, user_id: str, name: str, password: str, *, role: str = "user",
                quota_total: int = 0, env_file: str | None = None,
                port: int | None = None) -> None:
    conn.execute(
        """INSERT INTO users(id, name, role, password_hash, env_file, port,
                             quota_total, quota_used, enabled, created_at)
           VALUES(?,?,?,?,?,?,?,0,1,?)""",
        (user_id, name, role, hash_password(password), env_file, port, quota_total, now()))


def get_user(conn, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM users ORDER BY (role = 'admin') DESC, id"))


def set_password(conn, user_id: str, password: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))


def set_quota(conn, user_id: str, total: int) -> None:
    conn.execute("UPDATE users SET quota_total = ? WHERE id = ?", (int(total), user_id))


def add_quota(conn, user_id: str, delta: int) -> None:
    """审批通过时调用。不限额账号（-1）保持不限额。"""
    conn.execute(
        "UPDATE users SET quota_total = CASE WHEN quota_total < 0 THEN -1 "
        "ELSE quota_total + ? END WHERE id = ?", (int(delta), user_id))


def set_enabled(conn, user_id: str, on: bool) -> None:
    conn.execute("UPDATE users SET enabled = ? WHERE id = ?", (1 if on else 0, user_id))


def touch_login(conn, user_id: str) -> None:
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user_id))


def consume_quota(conn, user_id: str) -> bool:
    """核验开始时扣一次配额。

    用条件更新一条语句完成"检查并扣减"，避免两个请求同时读到余量。
    返回 False 表示额度已用尽（不限额账号永远 True）。
    """
    cur = conn.execute(
        """UPDATE users SET quota_used = quota_used + 1
            WHERE id = ? AND (quota_total < 0 OR quota_used < quota_total)""", (user_id,))
    return cur.rowcount == 1


def refund_quota(conn, user_id: str) -> None:
    """核验在真正开跑之前就失败时把预扣的那次还回去。"""
    conn.execute("UPDATE users SET quota_used = MAX(0, quota_used - 1) WHERE id = ?", (user_id,))


# ── 会话 ──────────────────────────────────────────────────────────────────
def create_session(conn, user_id: str, *, ip: str = "", ua: str = "",
                   seconds: int = SESSION_SECONDS) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO sessions(token, user_id, created_at, expires_at, ip, ua) "
                 "VALUES(?,?,?,?,?,?)", (token, user_id, now(), expires, ip, ua[:200]))
    purge_sessions(conn)
    return token


def session_user(conn, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?""", (token, now())).fetchone()
    if row is None:
        return None
    if not row["enabled"]:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return row


def delete_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_sessions(conn) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))


def count_sessions(conn) -> int:
    row = conn.execute("SELECT COUNT(*) c FROM sessions WHERE expires_at > ?", (now(),)).fetchone()
    return row["c"] if row else 0


# ── 用量 ──────────────────────────────────────────────────────────────────
def record_usage(conn, user_id: str, *, kind: str, claims: int = 0, seconds: float = 0,
                 usage: dict | None = None, ok: bool = True) -> None:
    """工作进程在完成任务后追加一条。usage 是 usage.py 的 since() 结果（Dict）。"""
    u = usage or {}
    ds = u.get("deepseek") or {}
    gl = u.get("glm") or {}
    yuan = None
    if isinstance(ds.get("yuan"), (int, float)) or isinstance(gl.get("yuan"), (int, float)):
        yuan = round(float(ds.get("yuan") or 0) + float(gl.get("yuan") or 0), 4)
    conn.execute(
        """INSERT INTO usage_log(user_id, at, kind, claims, seconds, ds_calls, ds_in,
                                 ds_cached, ds_out, glm_searches, yuan, ok)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, now(), kind, int(claims or 0), float(seconds or 0),
         int(ds.get("calls") or 0), int(ds.get("in") or 0), int(ds.get("cached") or 0),
         int(ds.get("out") or 0), int(gl.get("searches") or 0), yuan, 1 if ok else 0))


def user_usage(conn, user_id: str, *, days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        """SELECT COUNT(*) runs, COALESCE(SUM(claims),0) claims, COALESCE(SUM(seconds),0) seconds,
                  COALESCE(SUM(ds_in+ds_out),0) tokens, COALESCE(SUM(glm_searches),0) searches,
                  COALESCE(SUM(yuan),0) yuan
             FROM usage_log WHERE user_id = ? AND at >= ? AND ok = 1""",
        (user_id, since)).fetchone()
    return dict(row) if row else {}


def usage_daily(conn, *, days: int = 14, user_id: str | None = None) -> list[dict]:
    """按天聚合，供看板画趋势。"""
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    sql = ("SELECT substr(at,1,10) day, COUNT(*) runs, COALESCE(SUM(claims),0) claims, "
           "COALESCE(SUM(yuan),0) yuan, COALESCE(SUM(glm_searches),0) searches "
           "FROM usage_log WHERE at >= ? AND ok = 1")
    args: list = [since]
    if user_id:
        sql += " AND user_id = ?"
        args.append(user_id)
    sql += " GROUP BY day ORDER BY day"
    rows = {r["day"]: dict(r) for r in conn.execute(sql, args)}
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append(rows.get(d, {"day": d, "runs": 0, "claims": 0, "yuan": 0.0, "searches": 0}))
    return out


def usage_by_user(conn, *, days: int = 30) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return [dict(r) for r in conn.execute(
        """SELECT u.id, u.name, u.role, u.quota_total, u.quota_used,
                  COUNT(l.id) runs, COALESCE(SUM(l.claims),0) claims,
                  COALESCE(SUM(l.yuan),0) yuan, MAX(l.at) last_at
             FROM users u LEFT JOIN usage_log l
               ON l.user_id = u.id AND l.at >= ? AND l.ok = 1
            GROUP BY u.id ORDER BY (u.role='admin') DESC, u.id""", (since,))]


# ── 配额申请 ──────────────────────────────────────────────────────────────
def create_request(conn, user_id: str, amount: int, reason: str) -> int:
    # 同一个人已有待审申请时不重复建，避免 admin 看到一堆同样的条目
    exist = conn.execute("SELECT id FROM quota_requests WHERE user_id = ? AND status = 'pending'",
                         (user_id,)).fetchone()
    if exist:
        return exist["id"]
    cur = conn.execute(
        "INSERT INTO quota_requests(user_id, amount, reason, created_at) VALUES(?,?,?,?)",
        (user_id, int(amount), reason[:500], now()))
    return cur.lastrowid


def list_requests(conn, status: str | None = "pending", limit: int = 200) -> list[dict]:
    sql = ("SELECT r.*, u.name AS user_name FROM quota_requests r "
           "LEFT JOIN users u ON u.id = r.user_id ")
    args: list = []
    if status:
        sql += "WHERE r.status = ? "
        args.append(status)
    sql += "ORDER BY r.created_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def decide_request(conn, req_id: int, *, approve: bool, admin_id: str, note: str = "") -> bool:
    """审批。已处理过的不再重复生效。"""
    row = conn.execute("SELECT * FROM quota_requests WHERE id = ?", (req_id,)).fetchone()
    if not row or row["status"] != "pending":
        return False
    conn.execute("UPDATE quota_requests SET status = ?, decided_at = ?, decided_by = ?, note = ? "
                 "WHERE id = ?",
                 ("approved" if approve else "rejected", now(), admin_id, note[:500], req_id))
    if approve:
        add_quota(conn, row["user_id"], row["amount"])
    return True


def pending_count(conn) -> int:
    row = conn.execute("SELECT COUNT(*) c FROM quota_requests WHERE status = 'pending'").fetchone()
    return row["c"] if row else 0


# ── 访问统计 ──────────────────────────────────────────────────────────────
def record_access(conn, *, user_id: str | None, ip: str, method: str, path: str, status: int) -> None:
    conn.execute("INSERT INTO access_log(at, user_id, ip, method, path, status) VALUES(?,?,?,?,?,?)",
                 (now(), user_id, ip, method, path[:300], int(status)))


def access_summary(conn, *, days: int = 14) -> dict:
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    today = datetime.now().strftime("%Y-%m-%d 00:00:00")
    def one(sql: str, args: tuple = ()) -> dict:
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else {}          # sqlite3.Row 没有 .get()，先转成 dict
    total = one("SELECT COUNT(*) c, COUNT(DISTINCT ip) ips FROM access_log").get("c") or 0
    d_total = one("SELECT COUNT(*) c, COUNT(DISTINCT ip) ips FROM access_log WHERE at >= ?",
                  (today,))
    pages = one("SELECT COUNT(*) c FROM access_log WHERE at >= ? AND path NOT LIKE '/api/%'",
                (since,))["c"] or 0
    apis = one("SELECT COUNT(*) c FROM access_log WHERE at >= ? AND path LIKE '/api/%'",
               (since,))["c"] or 0
    # 趋势（含零值补齐，图不会断）
    rows = {r["day"]: r["c"] for r in conn.execute(
        "SELECT substr(at,1,10) day, COUNT(*) c FROM access_log WHERE at >= ? GROUP BY day", (since,))}
    trend = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append({"day": d, "views": rows.get(d, 0)})
    return {"total_views": total, "today_views": d_total.get("c") or 0,
            "unique_ips": d_total.get("ips") or 0,
            "page_views": pages, "api_calls": apis,
            "online": count_sessions(conn), "trend": trend}


def prune_access(conn, keep_days: int = 90) -> None:
    """访问流水只留最近 N 天，免得库里越积越大。"""
    cut = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d 00:00:00")
    conn.execute("DELETE FROM access_log WHERE at < ?", (cut,))


# ── 命令行：初始建库、加账号、改密码、调配额 ────────────────────────────────
def _cli() -> int:
    import argparse
    import sys
    # --db 同时挂到顶层和每个子命令：`store.py --db X init` 与 `store.py init --db X` 都认
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="",
                        help="库文件路径（默认取 UNDERBOX_DB，否则 ../users/underbox.db）")
    ap = argparse.ArgumentParser(description="underbox 账户库管理", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", parents=[common], help="建库建表（幂等）")
    sub.add_parser("list", parents=[common], help="列出账号")
    p_add = sub.add_parser("adduser", parents=[common], help="新增账号")
    p_add.add_argument("id")
    p_add.add_argument("--name", default="")
    p_add.add_argument("--password", required=True)
    p_add.add_argument("--role", default="user", choices=["user", "admin"])
    p_add.add_argument("--quota", type=int, default=20, help="配额次数；-1 表示不限")
    p_add.add_argument("--env-file", default="")
    p_add.add_argument("--port", type=int, default=0)
    p_pw = sub.add_parser("passwd", parents=[common], help="改密码")
    p_pw.add_argument("id")
    p_pw.add_argument("password")
    p_q = sub.add_parser("setquota", parents=[common], help="设配额")
    p_q.add_argument("id")
    p_q.add_argument("total", type=int)
    args = ap.parse_args()
    path = init(args.db) if args.db else init()

    if args.cmd == "init":
        print(f"库已就绪：{path}")
        return 0
    conn = connect(args.db or None)
    try:
        if args.cmd == "list":
            for u in list_users(conn):
                q = "不限" if u["quota_total"] < 0 else f"{u['quota_used']}/{u['quota_total']}"
                print(f"  {u['id']:<18} {u['role']:<6} 配额 {q:<10} "
                      f"{'启用' if u['enabled'] else '停用':<4} {u['name']}")
        elif args.cmd == "adduser":
            if get_user(conn, args.id):
                print(f"账号已存在：{args.id}", file=sys.stderr)
                return 1
            create_user(conn, args.id, args.name or args.id, args.password, role=args.role,
                        quota_total=args.quota, env_file=args.env_file or None,
                        port=args.port or None)
            conn.commit()
            print(f"已新增 {args.id}（{args.role}，配额 {args.quota}）")
        elif args.cmd == "passwd":
            set_password(conn, args.id, args.password)
            conn.commit()
            print(f"已更新 {args.id} 的密码")
        elif args.cmd == "setquota":
            set_quota(conn, args.id, args.total)
            conn.commit()
            print(f"已把 {args.id} 的配额设为 {args.total}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

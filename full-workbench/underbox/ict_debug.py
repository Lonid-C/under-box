"""Temporary, isolated browser sessions for the ICT login-free debug switch."""
import os
import secrets
import time
import store

TTL = 86400
SAMPLE = ('个人简历（虚构调试示例）\n个人信息\n姓名：林示例\n身份背景：示例大学\n竞赛经历\n'
          '2025 全国大学生数学建模竞赛（CUMCM）省级二等奖，队员。\n'
          '2024 校级程序设计竞赛二等奖，队员。\n以上人员与经历均为虚构，仅用于调试。')

def quotas_unlimited():
    return os.environ.get('UNDERBOX_ICT_UNLIMITED') == '1' and (
        os.environ.get('UNDERBOX_ICT_ENABLE') == '1' or os.environ.get('UNDERBOX_ICT_RECALL') == '1')

def enabled():
    return os.environ.get('UNDERBOX_ICT_DEBUG_GUEST') == '1'

def initialize(db):
    with store.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS ict_debug_users('
                     'user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,'
                     'expires_at REAL NOT NULL)')
        conn.commit()

def resolve(db, user):
    if user is None:
        return None
    with store.connect(db) as conn:
        row = conn.execute('SELECT expires_at FROM ict_debug_users WHERE user_id=?', (user['id'],)).fetchone()
    if row is None:
        return user
    if not enabled() or row['expires_at'] <= time.time():
        return None
    return {**user, '_ict_debug': True}

def create(accounts, ip, ua):
    if not enabled():
        raise ValueError('debug_mode_disabled')
    if not accounts.allow_attempt(ip):
        raise ValueError('debug_session_rate_limited')
    try:
        quota = max(1, min(50, int(os.environ.get('UNDERBOX_ICT_DEBUG_QUOTA', '5'))))
    except ValueError:
        quota = 5
    if quotas_unlimited():quota = -1
    uid = 'ict-debug-' + secrets.token_hex(16)
    with store.connect(accounts.path) as conn:
        store.create_user(conn, uid, '临时调试用户', secrets.token_urlsafe(48), role='user', quota_total=quota)
        conn.execute('INSERT INTO ict_debug_users VALUES(?,?)', (uid, time.time() + TTL))
        token = store.create_session(conn, uid, ip=ip, ua=ua, seconds=TTL)
        user = dict(store.get_user(conn, uid))
        conn.commit()
    return {**user, '_ict_debug': True}, token

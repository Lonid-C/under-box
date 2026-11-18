"""Pace all ICT GLM calls and retries in the shared verification worker."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import math
import os
import re
from threading import Condition
import time

from .search import SearchUnavailable

MIN_INTERVAL = 2.5
MAX_WAIT = 90.0
RATE_BACKOFF = (10.0, 20.0, 40.0, 60.0)
ACCOUNT_BACKOFF = 300.0
_ACCOUNT_ERRORS = {
    "1113": "智谱搜索余额不足或资源包不可用，请检查智谱账户",
    "1308": "智谱搜索用量已达上限，请等待额度恢复",
    "1309": "智谱账户套餐已到期",
    "1310": "智谱搜索周期额度已用尽，请等待额度恢复",
    "1311": "当前智谱套餐没有此接口权限",
    "1313": "智谱账户请求受平台使用策略限制，请查看控制台",
    "1314": "智谱账户的企业套餐已失效",
    "1315": "当前智谱密钥不适用于该接口",
    **{str(n): "智谱账户用量或消费已达上限，请查看控制台" for n in range(1316, 1322)},
}


def enabled():
    return os.environ.get("UNDERBOX_ICT_GLM_QUEUE") == "1"


class ZhipuSearchError(SearchUnavailable):
    def __init__(self, reason, *, status=None, code="", kind="provider", sent=True, retry_after=0):
        self.reason = reason
        self.status = status
        self.code = code
        self.kind = kind
        self.sent = sent
        self.retry_after = retry_after
        reference = " / ".join(part for part in (f"HTTP {status}" if status else "", code) if part)
        message = reason if sent else "请求未发出，" + reason
        super().__init__(f"{message}（{reference}）" if reference else message)

    def deferred(self):
        return ZhipuSearchError(self.reason, status=self.status, code=self.code,
                                kind="deferred", sent=False)


def retry_after_seconds(value):
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError, OSError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


def provider_error(response):
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = {}
    details = data.get("error") if isinstance(data, dict) else {}
    details = details if isinstance(details, dict) else {}
    code = str(details.get("code") or "").strip()
    if not re.fullmatch(r"\d{4}", code, flags=re.ASCII):
        code = ""
    message = str(details.get("message") or response.text[:300])
    wait = retry_after_seconds(response.headers.get("Retry-After"))
    if status in (401, 403):
        kind, reason = "account", "智谱搜索鉴权失败或接口未获授权"
    elif status == 402 or code == "1113" or status == 429 and any(
        label in message for label in ("余额不足", "无可用资源包", "请充值")
    ):
        kind, reason = "account", _ACCOUNT_ERRORS["1113"]
    elif status == 429 and code in _ACCOUNT_ERRORS:
        kind, reason = "account", _ACCOUNT_ERRORS[code]
    elif status == 429:
        kind = "rate"
        reason = {"1302": "智谱搜索达到请求速率限制", "1305": "智谱搜索服务繁忙"}.get(
            code, "智谱搜索暂时拒绝请求，需查看业务错误码")
    else:
        kind, reason = "provider", "智谱搜索服务端故障" if status >= 500 else "智谱搜索请求被拒绝"
    return ZhipuSearchError(reason, status=status, code=code, kind=kind, retry_after=wait)


class SearchQueue:
    """One in-flight request per worker; an upstream 429 cools down the queue."""
    def __init__(self):
        self.condition = Condition()
        self.waiting = deque()
        self.busy = False
        self.next_at = 0.0
        self.cooldown_until = 0.0
        self.last_rate_error = None
        self.rate_failures = 0
        self.account_blocks = {}

    @contextmanager
    def slot(self, credential_id, *, max_wait=MAX_WAIT):
        deadline = time.monotonic() + max_wait
        ticket = object()
        with self.condition:
            self.waiting.append(ticket)
            try:
                while True:
                    now = time.monotonic()
                    self.account_blocks = {k: v for k, v in self.account_blocks.items() if v[0] > now}
                    block = self.account_blocks.get(credential_id)
                    if block:
                        raise block[1].deferred()
                    ready_at = max(self.next_at, self.cooldown_until)
                    if now >= deadline or ready_at >= deadline:
                        if self.cooldown_until > now and self.last_rate_error:
                            raise self.last_rate_error.deferred()
                        raise ZhipuSearchError("智谱搜索排队超时", kind="deferred", sent=False)
                    if self.waiting[0] is ticket and not self.busy and now >= ready_at:
                        self.next_at = now + MIN_INTERVAL
                        self.busy = True
                        break
                    wake_at = ready_at if self.waiting[0] is ticket and not self.busy else deadline
                    self.condition.wait(max(0, min(wake_at, deadline) - now))
            finally:
                self.waiting.remove(ticket)
                self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.busy = False
                self.condition.notify_all()

    def reject(self, credential_id, error):
        now = time.monotonic()
        with self.condition:
            if error.kind == "account":
                self.account_blocks[credential_id] = (now + max(ACCOUNT_BACKOFF, error.retry_after), error)
            elif error.kind == "rate":
                self.last_rate_error = error
                delay = RATE_BACKOFF[min(self.rate_failures, len(RATE_BACKOFF) - 1)]
                self.rate_failures += 1
                self.cooldown_until = now + max(delay, error.retry_after)
            self.condition.notify_all()

    def success(self):
        with self.condition:
            self.rate_failures = 0
            self.last_rate_error = None


QUEUE = SearchQueue()

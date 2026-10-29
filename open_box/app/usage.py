"""额度用量记账：这一次查询分别烧了 DeepSeek 与 GLM 多少。

只做两件事：各调用点在拿到供应商响应后 record_*()；serve.py 订阅变化并推给页面。
llm.py / search.py 记录供应商用量，serve.py 为 HTTP 任务创建独立作用域。

口径：
  · DeepSeek = 大模型 token（输入 / 缓存命中 / 输出），响应里的 usage 原样累加。
  · GLM      = 智谱 web_search 按**次**计费（单价见 search.py：std 0.01 / pro 0.03 /
               sogou 0.05 元），外加 provider=zhipu 时 LLM 的 token。
  · 折算成元只在单价已知时给：搜索单价内置；DeepSeek / GLM 的 token 单价随官方调价，
    不硬编码，用环境变量给（元 / 百万 token）：
        DEEPSEEK_PRICE_IN / DEEPSEEK_PRICE_IN_CACHED / DEEPSEEK_PRICE_OUT          （高峰）
        DEEPSEEK_PRICE_IN_OFFPEAK / ..._IN_CACHED_OFFPEAK / ..._OUT_OFFPEAK      （空闲，未配则同高峰）
      DeepSeek 按时段计价：北京时间周一至周五 9-12、14-18 为高峰，其余为空闲（空闲价为高峰一半）。
      法定节假日也算空闲，但这里不内置节假日表，节假日工作日会按高峰估，略偏高。
        ZHIPU_PRICE_IN / ZHIPU_PRICE_OUT
    没配就只显示 token，不编造金额。

线程：每个 HTTP 任务有独立计量作用域；UsageExecutor 显式把上下文传入线程池。
后台任务和 HTTP 流分别释放引用，客户端退出后剩余任务也不会串入其他账户。
"""
from __future__ import annotations

import os
import threading
import contextvars
import math
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

# 智谱 web_search 单价（元 / 次），与 search.py 注释同一来源（open.bigmodel.cn/pricing）
SEARCH_PRICE = {"search_std": 0.01, "search_pro": 0.03,
                "search_pro_sogou": 0.05, "search_pro_quark": 0.05}


def _price(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else None
        return value if value is not None and math.isfinite(value) and value >= 0 else None
    except ValueError:
        return None


def _is_peak(now: datetime | None = None) -> bool:
    beijing = timezone(timedelta(hours=8))
    t = now or datetime.now(beijing)
    t = t.astimezone(beijing) if t.tzinfo else t.replace(tzinfo=beijing)
    return t.weekday() < 5 and (9 <= t.hour < 12 or 14 <= t.hour < 18)


def _ds_price(name: str) -> float | None:
    if not _is_peak():
        off = _price(name + "_OFFPEAK")
        if off is not None:
            return off
    return _price(name)


def _blank() -> dict:
    return {"deepseek": {"calls": 0, "in": 0, "cached": 0, "out": 0, "tokenYuan": 0.0, "unpricedCalls": 0},
            "glm": {"calls": 0, "in": 0, "out": 0,
                    "searches": 0, "searchYuan": 0.0, "byEngine": {}, "tokenYuan": 0.0, "unpricedCalls": 0}}


class UsageMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = _blank()
        self._buckets = {"global": self._data}
        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self._owners: dict[str, int] = {}
        self._scope = contextvars.ContextVar("usage_scope", default="global")

    def open_scope(self, owners: int = 1) -> str:
        scope = uuid.uuid4().hex
        with self._lock:
            self._buckets[scope] = _blank()
            self._owners[scope] = owners
        return scope

    @contextmanager
    def bind(self, scope: str):
        token = self._scope.set(scope)
        try:
            yield
        finally:
            self._scope.reset(token)

    def release_scope(self, scope: str) -> None:
        with self._lock:
            if scope not in self._owners:
                return
            self._owners[scope] -= 1
            if self._owners[scope] <= 0:
                self._owners.pop(scope, None)
                self._buckets.pop(scope, None)
                self._listeners.pop(scope, None)

    def run_in_scope(self, scope: str, fn, *args):
        try:
            with self.bind(scope):
                return fn(*args)
        finally:
            self.release_scope(scope)

    # -- 记账 -----------------------------------------------------------
    def record_llm(self, provider: str, usage: dict | None) -> None:
        """provider：deepseek / zhipu；其余供应商不计入这两栏。usage 为响应里的 usage 对象。"""
        side = {"deepseek": "deepseek", "zhipu": "glm"}.get(provider)
        if not side:
            return
        reported = isinstance(usage, dict) and "prompt_tokens" in usage and "completion_tokens" in usage
        usage = usage if reported else {}
        def count(value):
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError, OverflowError):
                return 0
        prompt = count(usage.get("prompt_tokens"))
        out = count(usage.get("completion_tokens"))
        # DeepSeek 把缓存命中单列（prompt_cache_hit_tokens）；智谱放在 prompt_tokens_details.cached_tokens
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cached = min(prompt, count(usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens")))
        p_in, p_hit, p_out = (_ds_price("DEEPSEEK_PRICE_IN"), _ds_price("DEEPSEEK_PRICE_IN_CACHED"), _ds_price("DEEPSEEK_PRICE_OUT")) if side == "deepseek" else (_price("ZHIPU_PRICE_IN"), None, _price("ZHIPU_PRICE_OUT"))
        scope = self._scope.get()
        with self._lock:
            bucket = self._buckets.get(scope)
            if bucket is None:
                return
            d = bucket[side]
            d["calls"] += 1
            d["in"] += prompt
            d["out"] += out
            if side == "deepseek":
                d["cached"] += cached
            # 按调用当时的价格累计，跨高峰/空闲时段的任务不会重新给旧 token 计价。
            if reported and p_in is not None and p_out is not None:
                d["tokenYuan"] += ((prompt - cached) * p_in + cached * (p_hit if p_hit is not None else p_in) + out * p_out) / 1e6
            else:
                d["unpricedCalls"] += 1
        self._notify(scope)

    def record_search(self, engine: str) -> None:
        """一次成功的智谱搜索请求（按次计费）。"""
        yuan = SEARCH_PRICE.get(engine, 0.0)
        scope = self._scope.get()
        with self._lock:
            bucket = self._buckets.get(scope)
            if bucket is None:
                return
            g = bucket["glm"]
            g["searches"] += 1
            g["searchYuan"] = round(g["searchYuan"] + yuan, 4)
            g["byEngine"][engine] = g["byEngine"].get(engine, 0) + 1
        self._notify(scope)

    # -- 读取 -----------------------------------------------------------
    def snapshot(self, scope: str | None = None) -> dict:
        with self._lock:
            return _decorate(_copy(self._buckets.get(scope or self._scope.get(), _blank())))

    def mark(self, scope: str | None = None) -> dict:
        """取当前累计值作基线，配合 since() 得到「这一次」的增量。"""
        with self._lock:
            key = scope or self._scope.get()
            return {**_copy(self._buckets.get(key, _blank())), "_scope": key}

    def since(self, base: dict) -> dict:
        with self._lock:
            return _decorate(_diff(_copy(self._buckets.get(base.get("_scope", self._scope.get()), _blank())), base))

    # -- 订阅 -----------------------------------------------------------
    def subscribe(self, fn: Callable[[], None], scope: str | None = None) -> Callable[[], None]:
        key = scope or self._scope.get()
        with self._lock:
            self._listeners.setdefault(key, []).append(fn)

        def off() -> None:
            with self._lock:
                listeners = self._listeners.get(key, [])
                if fn in listeners:
                    listeners.remove(fn)
        return off

    def _notify(self, scope: str) -> None:
        with self._lock:
            fns = list(self._listeners.get(scope, []))
        for fn in fns:
            try:
                fn()
            except Exception:
                pass                      # 记账通知绝不能拖垮业务调用


def _copy(d: dict) -> dict:
    out = {k: dict(v) for k, v in d.items()}
    out["glm"]["byEngine"] = dict(d["glm"]["byEngine"])
    return out


def _diff(now: dict, base: dict) -> dict:
    for side, vals in now.items():
        for k, v in vals.items():
            if k == "byEngine":
                prev = base[side].get(k, {})
                vals[k] = {e: n - prev.get(e, 0) for e, n in v.items() if n - prev.get(e, 0)}
            else:
                vals[k] = round(v - base[side].get(k, 0), 10) if isinstance(v, float) else v - base[side].get(k, 0)
    return now


def _decorate(d: dict) -> dict:
    """补上总 token 与折算金额（单价已知才给，否则 None）。"""
    ds, gl = d["deepseek"], d["glm"]
    ds["tokens"] = ds["in"] + ds["out"]
    gl["tokens"] = gl["in"] + gl["out"]

    ds["yuan"] = None if ds["unpricedCalls"] else round(ds["tokenYuan"], 4)
    token_yuan = None if gl["unpricedCalls"] else round(gl["tokenYuan"], 4)
    gl["tokenYuan"] = token_yuan
    gl["yuan"] = round(gl["searchYuan"] + token_yuan, 4) if token_yuan is not None else None
    return d


METER = UsageMeter()


class UsageExecutor(ThreadPoolExecutor):
    """线程池不自动继承 ContextVar；每个任务使用独立的复制，支持嵌套并发。"""
    def submit(self, fn, /, *args, **kwargs):
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)

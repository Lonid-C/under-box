"""额度用量记账：这一次查询分别烧了 DeepSeek 与 GLM 多少。

只做两件事：各调用点在拿到供应商响应后 record_*()；serve.py 订阅变化并推给页面。
业务代码不认识这个模块——只有 llm.py / search.py / serve.py 三处接线。

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

线程：流水线里有线程池，contextvars 传不过去，所以用进程级单例 + 锁。
一次只服务一个核验任务（本服务的使用方式）；并发任务的用量会混在一起，
mark() 取基线、since() 取增量，只保证单任务口径。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

# 智谱 web_search 单价（元 / 次），与 search.py 注释同一来源（open.bigmodel.cn/pricing）
SEARCH_PRICE = {"search_std": 0.01, "search_pro": 0.03,
                "search_pro_sogou": 0.05, "search_pro_quark": 0.05}


def _price(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _is_peak(now: datetime | None = None) -> bool:
    t = (now or datetime.now(timezone(timedelta(hours=8))))
    return t.weekday() < 5 and (9 <= t.hour < 12 or 14 <= t.hour < 18)


def _ds_price(name: str) -> float | None:
    if not _is_peak():
        off = _price(name + "_OFFPEAK")
        if off is not None:
            return off
    return _price(name)


def _blank() -> dict:
    return {"deepseek": {"calls": 0, "in": 0, "cached": 0, "out": 0},
            "glm": {"calls": 0, "in": 0, "out": 0,
                    "searches": 0, "searchYuan": 0.0, "byEngine": {}}}


class UsageMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = _blank()
        self._listeners: list[Callable[[], None]] = []

    # -- 记账 -----------------------------------------------------------
    def record_llm(self, provider: str, usage: dict | None) -> None:
        """provider：deepseek / zhipu；其余供应商不计入这两栏。usage 为响应里的 usage 对象。"""
        side = {"deepseek": "deepseek", "zhipu": "glm"}.get(provider)
        if not side or not isinstance(usage, dict):
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        out = int(usage.get("completion_tokens") or 0)
        # DeepSeek 把缓存命中单列（prompt_cache_hit_tokens）；智谱放在 prompt_tokens_details.cached_tokens
        cached = int(usage.get("prompt_cache_hit_tokens")
                     or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        with self._lock:
            d = self._data[side]
            d["calls"] += 1
            d["in"] += prompt
            d["out"] += out
            if side == "deepseek":
                d["cached"] += cached
        self._notify()

    def record_search(self, engine: str) -> None:
        """一次成功的智谱搜索请求（按次计费）。"""
        yuan = SEARCH_PRICE.get(engine, 0.0)
        with self._lock:
            g = self._data["glm"]
            g["searches"] += 1
            g["searchYuan"] = round(g["searchYuan"] + yuan, 4)
            g["byEngine"][engine] = g["byEngine"].get(engine, 0) + 1
        self._notify()

    # -- 读取 -----------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return _decorate(_copy(self._data))

    def mark(self) -> dict:
        """取当前累计值作基线，配合 since() 得到「这一次」的增量。"""
        with self._lock:
            return _copy(self._data)

    def since(self, base: dict) -> dict:
        with self._lock:
            return _decorate(_diff(_copy(self._data), base))

    # -- 订阅 -----------------------------------------------------------
    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def off() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)
        return off

    def _notify(self) -> None:
        with self._lock:
            fns = list(self._listeners)
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
                vals[k] = round(v - base[side].get(k, 0), 4) if isinstance(v, float) else v - base[side].get(k, 0)
    return now


def _decorate(d: dict) -> dict:
    """补上总 token 与折算金额（单价已知才给，否则 None）。"""
    ds, gl = d["deepseek"], d["glm"]
    ds["tokens"] = ds["in"] + ds["out"]
    gl["tokens"] = gl["in"] + gl["out"]

    p_in, p_hit, p_out = (_ds_price("DEEPSEEK_PRICE_IN"), _ds_price("DEEPSEEK_PRICE_IN_CACHED"),
                          _ds_price("DEEPSEEK_PRICE_OUT"))
    if p_in is not None and p_out is not None:
        hit = ds["cached"]
        ds["yuan"] = round(((ds["in"] - hit) * p_in + hit * (p_hit if p_hit is not None else p_in)
                            + ds["out"] * p_out) / 1e6, 4)
    else:
        ds["yuan"] = None

    z_in, z_out = _price("ZHIPU_PRICE_IN"), _price("ZHIPU_PRICE_OUT")
    token_yuan = (round((gl["in"] * z_in + gl["out"] * z_out) / 1e6, 4)
                  if z_in is not None and z_out is not None else (0.0 if not gl["tokens"] else None))
    gl["tokenYuan"] = token_yuan
    gl["yuan"] = round(gl["searchYuan"] + token_yuan, 4) if token_yuan is not None else None
    return d


METER = UsageMeter()

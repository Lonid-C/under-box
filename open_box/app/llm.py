"""LLM 封装。业务代码只认识 complete_json()，不认识任何一家 API。

默认供应商：DeepSeek（OpenAI 兼容格式）。
换供应商只需要改 PROVIDERS 表或换一个实现类，split/collect/questions 一行都不用动。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Protocol


class LLMUnavailable(RuntimeError):
    """没配置好，或供应商明确拒绝（鉴权失败、余额不足、参数非法）。不该重试。"""


class LLMTransient(RuntimeError):
    """限流或服务端故障。退避后可重试。"""


# --------------------------------------------------------------------------
# 供应商预设
# --------------------------------------------------------------------------
# 端点与模型名核对自 api-docs.deepseek.com（2026-09）：
#   base_url  https://api.deepseek.com          对话端点 /chat/completions
#   模型字符串  deepseek-flash（官方推荐）、deepseek-v4-pro
#
# 注意区分"API model 字符串"和"底层模型版本"：
#   deepseek-flash  →  当前由 DeepSeek-V4.1-Flash 承接
# 官方没有放出 deepseek-v4.1-flash 这样的调用名，所以这里填 deepseek-flash 而不是版本号——
# 它会跟着官方升级自动指向最新的 Flash，写死版本号反而会在下次换代时失效。
#   旧名 deepseek-v4-flash / deepseek-v4-flash-vision-exp 仍被接受，但对应模型已退役，
#   请求同样由 V4.1-Flash 承接并按 Flash 价计费；
#   更早的 deepseek-chat / deepseek-reasoner 属于历史名称，不在当前列表里。
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
        "key_envs": ("DEEPSEEK_API_KEY", "LLM_API_KEY"),
        "json_mode": True,
        # deepseek-flash **默认开启思考**，正文之外的 reasoning_content 也占 max_tokens。
        # 实测：max_tokens=32 时 32 个 token 全花在 reasoning 上，content 返回空串、
        # finish_reason=length——看起来像"JSON 模式偶发空内容"，其实是预算被吃光。
        # 本场景是结构化抽取 + temperature=0，要的是确定性与可复现，不是推理过程，
        # 所以默认关掉思考。要开就用 LLM_THINKING=enabled。
        "thinking": "disabled",
    },
    "openai-compatible": {
        "endpoint": "",
        "model": "",
        "key_envs": ("LLM_API_KEY",),
        "json_mode": True,
        # 别家不认 DeepSeek 的 thinking 字段，缺省不发。
        "thinking": None,
    },
}

# DeepSeek 的 JSON Output 要求提示词里出现 "json" 字样，否则可能不走结构化输出
JSON_HINT = '\n\n以 json 格式输出，例如：{"question": "……？"} 或 [{"id": "c01", ...}]。'

RETRY_STATUS = {429, 500, 502, 503, 504}
FATAL_STATUS = {400: "请求体格式不合法", 401: "API key 无效或未授权",
                402: "账户余额不足", 422: "请求参数不合法"}


def extract_json(text: str) -> Any:
    """模型有时会裹 ```json 或加解释文字，这里只取第一段合法 JSON。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"模型输出不是合法 JSON：{text[:200]}")


class LLM(Protocol):
    def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any: ...


class NullLLM:
    """没有配置 key 时使用。调用即报错，避免静默编造内容。"""

    def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        raise LLMUnavailable(
            "未配置 LLM key。mock 模式不需要 LLM；live 模式请设置 DEEPSEEK_API_KEY"
            "（可选 LLM_MODEL，默认 deepseek-flash）。"
        )


class StubLLM:
    """测试用：按调用顺序吐出预置结果。"""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        self.calls.append((system, user))
        return self.responses.pop(0) if self.responses else []


class OpenAICompatLLM:
    """OpenAI 兼容形状的通用客户端（DeepSeek 用的就是这套报文）。

    - 429/5xx 退避重试；401/402/400/422 直接报错，不做无意义的重试
    - 开启 JSON Output 时按供应商要求保证提示词里出现 "json" 字样
    - DeepSeek 官方提示 JSON 模式偶发返回空内容，这里做一次带提示的重试
    """

    def __init__(self, endpoint: str = "", api_key: str = "", model: str = "",
                 *, json_mode: bool = True, timeout: float = 120.0,
                 max_retries: int = 3, provider: str = "openai-compatible",
                 thinking: str | None = None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.json_mode = json_mode
        self.timeout = timeout
        self.max_retries = max_retries
        self.provider = provider
        # None = 不发这个字段；"disabled"/"enabled" = 明确告诉 DeepSeek 要不要思考
        self.thinking = thinking
        self.calls = 0

    # -- 报文 ------------------------------------------------------------
    def _payload(self, system: str, user: str, max_tokens: int, nudge: bool = False) -> dict:
        if self.json_mode and "json" not in (system + user).lower():
            system = system + JSON_HINT
        if nudge:
            user = user + "\n\n（上一次返回为空，请直接输出 json，不要输出任何解释文字。）"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
        }
        if self.thinking in ("disabled", "enabled"):
            body["thinking"] = {"type": self.thinking}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _content(payload: dict) -> str:
        choices = payload.get("choices") or [{}]
        msg = choices[0].get("message") or {}
        # deepseek-reasoner 之类会同时给 reasoning_content，只取正式回答
        return (msg.get("content") or "").strip()

    def _post(self, body: dict) -> dict:
        import httpx
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = httpx.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=body, timeout=self.timeout,
                )
            except Exception as exc:                       # 网络层问题，可重试
                last_exc = exc
                time.sleep(min(2 ** attempt, 8))
                continue

            if r.status_code in FATAL_STATUS:
                raise LLMUnavailable(
                    f"{self.provider} 返回 {r.status_code}：{FATAL_STATUS[r.status_code]}。"
                    f"{r.text[:200]}")
            if r.status_code in RETRY_STATUS:
                last_exc = LLMTransient(f"{self.provider} 返回 {r.status_code}")
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 400:
                raise LLMUnavailable(f"{self.provider} 返回 {r.status_code}：{r.text[:200]}")
            return r.json()

        raise LLMTransient(f"{self.provider} 连续 {self.max_retries} 次未成功：{last_exc}")

    # -- 对外 ------------------------------------------------------------
    def complete_json(self, system: str, user: str, *, max_tokens: int = 4096) -> Any:
        """带自愈的 JSON 调用。三种可自愈的情况，各给一次机会：

          · finish_reason=length → **被截断了**。真实简历动辄拆出 20+ 条陈述，
            4096 token 根本装不下；截断后的 JSON 解析报错会让人误以为"模型不会说话"，
            实际是预算不够。加倍预算再来（实测 deepseek-flash 接受到 32000）。
          · content 为空          → 官方文档提示 JSON 模式偶发，补一次。
          · 解析失败              → 提示"只输出 json"再要一次。

        都不行才抛 LLMTransient。max_tokens 只是**上限**不是成本——
        实际计费按真实输出算，所以放开上限不花钱，只防截断。
        """
        if not (self.endpoint and self.api_key and self.model):
            raise LLMUnavailable(
                f"{self.provider} 配置不全：endpoint/api_key/model 三者都要有。")
        self.calls += 1
        ceiling = 24000
        last_err = ""
        for attempt in range(3):
            data = self._post(self._payload(system, user, max_tokens, nudge=attempt > 0))
            choice = (data.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "").strip()
            finish = choice.get("finish_reason")

            if finish == "length" and max_tokens < ceiling:
                max_tokens = min(max_tokens * 2, ceiling)
                last_err = "输出被 max_tokens 截断（已自动加倍预算重试）"
                continue
            if not text:
                last_err = "返回空内容"
                continue
            try:
                return extract_json(text)
            except ValueError as exc:
                last_err = str(exc)
                continue

        hint = "" if self.thinking == "disabled" else (
            " 若用的是带思考的模型（如 deepseek-flash 默认开思考），"
            "reasoning 会占满 max_tokens 让 content 变空——设 LLM_THINKING=disabled。")
        raise LLMTransient(f"{self.provider} {last_err}。{hint}")

    def ping(self) -> str:
        """一次最小调用，用来验证 key / 端点 / 模型名是否可用。"""
        got = self.complete_json(
            '你是一个连通性测试端点。只输出 json：{"ok": true}',
            "请回复 {\"ok\": true}", max_tokens=32)
        return json.dumps(got, ensure_ascii=False)


def build_llm(provider: str | None = None) -> LLM:
    """按环境变量装配。默认 DeepSeek。

    DEEPSEEK_API_KEY=sk-...            # 或 LLM_API_KEY
    LLM_MODEL=deepseek-flash           # 可选，默认 deepseek-flash
    LLM_ENDPOINT=...                   # 可选，自建/代理网关时覆盖
    LLM_PROVIDER=deepseek              # 可选
    LLM_THINKING=disabled              # 可选，默认按供应商（deepseek 默认 disabled）
    """
    name = (provider or os.environ.get("LLM_PROVIDER") or "deepseek").lower()
    preset = PROVIDERS.get(name, PROVIDERS["openai-compatible"])

    api_key = ""
    for env in preset["key_envs"]:
        if os.environ.get(env):
            api_key = os.environ[env]
            break
    if not api_key:
        return NullLLM()

    # LLM_THINKING 可以反过来覆盖预设；留空则用供应商默认
    thinking = (os.environ.get("LLM_THINKING") or "").strip().lower() or preset.get("thinking")
    if thinking not in ("disabled", "enabled"):
        thinking = preset.get("thinking")

    return OpenAICompatLLM(
        endpoint=os.environ.get("LLM_ENDPOINT") or preset["endpoint"],
        api_key=api_key,
        model=os.environ.get("LLM_MODEL") or preset["model"],
        json_mode=bool(preset["json_mode"]),
        provider=name,
        thinking=thinking,
    )

"""履历核验（open_box）。

这里做一件事：导入 `app.*` 的任何模块之前，先把 `.env` 读进 `os.environ`。

放在包初始化处而不是各个入口，是因为读环境变量的地方不止一处——
`llm.build_llm`、`search.build_searcher`（连模块级的 `UA` 都用），
以及 `pipeline` 里的 `MODE`。集中在这里就不会漏掉其中任何一个。

已有的环境变量优先，`.env` 只补缺。详见 `app/env.py`。
"""
from __future__ import annotations

from .env import load_env

load_env()


def _use_system_trust_store() -> None:
    """用操作系统的证书校验（macOS 钥匙串 / Windows 证书库）代替 certifi。

    实测 2026-10-07：华南理工等多所高校官网浏览器能开，程序却报"连接失败"。
    这些站点常常只下发了服务器证书、漏了中间证书；浏览器和系统会按证书里的
    AIA 地址自动补齐，Python 自带的 certifi 不会，于是 TLS 握手失败。
    truststore 让 Python 走系统校验——不是关闭校验，校验照样做，只是换成和
    浏览器一样的校验方式。没装 truststore 时保持原样。
    """
    import os
    if os.environ.get("DISABLE_TRUSTSTORE"):
        return
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass


_use_system_trust_store()

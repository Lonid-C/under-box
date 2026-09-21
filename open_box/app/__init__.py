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

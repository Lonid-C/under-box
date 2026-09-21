"""极小的 `.env` 读取器（只用标准库，不引入 python-dotenv）。

存在的理由：README 让用户 `cp .env.example .env` 然后把 key 填进去，
但代码里从来没有读过那个文件——写进去的 key 根本不会生效。
（验收第 11 节之外的一个缺口，构建时漏掉了。）

约定：**已经存在的环境变量优先**，`.env` 只补缺、不覆盖。
这样 `DEEPSEEK_API_KEY=… python -m app.cli verify` 这种一次性覆盖仍然说了算，
与 dsh 凭据分层"启动环境优先于存储文件"的口径一致。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / ".env"

# 认得 `KEY=value` 与 `export KEY=value`；`#` 开头的整行跳过。
# 不处理行内注释与多行值——.env.example 里没有这种写法，真出现会原样带进值里，
# 比"猜错了截断一半"更容易发现。
_QUOTES = ("'", '"')


def parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        out[key] = value
    return out


def load_env(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """把 .env 读进 os.environ，返回真正被写入的那些键。

    默认不覆盖已有环境变量——环境变量是操作者的显式意图，优先级最高。
    """
    env_file = Path(path) if path is not None else DEFAULT_ENV_FILE
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}          # 没有 .env 或读不动，就当没配过，不报错

    written: dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        if override or key not in os.environ:
            os.environ[key] = value
            written[key] = value
    return written

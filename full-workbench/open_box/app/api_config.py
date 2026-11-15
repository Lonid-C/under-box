"""Request-scoped search credentials; shared worker/DeepSeek environment is untouched."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchCredentials:
    api_key: str = field(repr=False)

    def __post_init__(self):
        if not self.api_key or not self.api_key.isascii() or any(c.isspace() for c in self.api_key):
            raise ValueError("账户搜索密钥配置无效")


_SEARCH = ContextVar("account_search_credentials", default=None)


def search_credentials() -> SearchCredentials | None:
    return _SEARCH.get()


@contextmanager
def bind_search_credentials(credentials: SearchCredentials | None):
    token = _SEARCH.set(credentials)
    try:
        yield
    finally:
        _SEARCH.reset(token)


# Institutional background/QA uses the basic index. This preference is scoped to
# the call and never changes another candidate's evidence-search configuration.
_BACKGROUND = ContextVar("underbox_background_search", default=False)


def is_background_search():
    return _BACKGROUND.get()


@contextmanager
def background_search():
    token = _BACKGROUND.set(True)
    try:
        yield
    finally:
        _BACKGROUND.reset(token)

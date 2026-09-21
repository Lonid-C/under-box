"""步骤 3：检索计划。

**这里的策略已经搬走了。** 原来是一堆 if-else 的查询模板，现在全在
`data/strategies.json`（策略档案）+ `app/strategy.py`（展开与排序）。

本文件只剩三件事：

1. **兼容旧的调用方式**——`plan_queries(claim, name)` 仍然返回 `list[Query]`，
   `collect.py` 与桥不用改一行。
2. **学校域名表**——`school_domain()` 是"学校名 → 域名"的唯一映射来源，
   `strategy` 与 `collect` 都从这儿取，避免两处口径分叉。
3. **预算常量的家**——硬上限定义在 `strategy.py`，档案只能给更小的值，不能突破。

要看策略设计读 `search-strategy/DESIGN.md`；要改策略改那个 JSON，不要回来加 if。
"""
from __future__ import annotations

import json
from pathlib import Path

from .schema import Claim
from .strategy import (MAX_PAGE_READS, MAX_SEARCHES, MAX_SECONDS,  # noqa: F401
                       Plan, Query, ResumeProfile, StrategyError,
                       build_plan, load_strategies, match_profiles)

DATA = Path(__file__).resolve().parent.parent / "data" / "schools.json"

# 微信公众号：只用搜索引擎已收录的文章链接，或候选人自己提供的链接（第 7 节）。
# 微信未对第三方开放文章检索接口，官方"搜一搜"只能单向推送自己的内容；
# 搜狗微信搜索没有对外 API，围绕它的生态是爬虫，属于本项目明令禁止的"搜狗绕行"。
WECHAT_HOST = "mp.weixin.qq.com"


def _load_schools() -> list[dict]:
    try:
        return json.loads(DATA.read_text(encoding="utf-8")).get("schools", [])
    except Exception:
        return []


def school_domain(name: str | None) -> str | None:
    """学校名 → 主域名。查不到就返回 None，退化为不带 site: 的普通查询。"""
    if not name:
        return None
    for s in _load_schools():
        names = [s["name"]] + list(s.get("aliases", []))
        if any(n and n in name for n in names):
            return s.get("domain")
    return None


def plan_queries(claim: Claim, candidate_name: str,
                 profile: ResumeProfile | dict | None = None,
                 resume_categories: list[str] | set[str] | None = None) -> list[Query]:
    """展开一条陈述的检索计划。

    profile 是模型读简历产出的画像（身份/行业/层级/技能）。不给就走兜底档案——
    兜底档案的预算**更低**：不知道去哪找时，广撒网只会烧预算换噪音。

    resume_categories 是整份简历出现过的类别集合。流水线里拆分完就有了，传进来能让
    档案匹配更准（在校生简历里的论文，照样该走在校生策略）。
    """
    return plan_with_notes(claim, candidate_name, profile, resume_categories).queries


def plan_with_notes(claim: Claim, candidate_name: str,
                    profile: ResumeProfile | dict | None = None,
                    resume_categories: list[str] | set[str] | None = None) -> Plan:
    """带诊断的版本：命中了哪个档案、哪些查询被预算裁掉、哪些模板因缺字段没展开。
    排查"为什么这条没搜到"时用这个，而不是读代码猜。"""
    if isinstance(profile, dict):
        profile = ResumeProfile.from_dict(profile)
    return build_plan(claim, candidate_name, profile, resume_categories=resume_categories)


def provided_urls(claim: Claim) -> list[str]:
    """候选人自己提供的链接。这是拿到公众号内容唯一合规且可靠的路径。"""
    raw = (claim.entities or {}).get("provided_urls") or []
    if isinstance(raw, str):
        raw = [raw]
    return [u.strip() for u in raw if isinstance(u, str) and u.strip().startswith("http")]

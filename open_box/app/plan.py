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
import re
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


_AMBIGUOUS_ALIASES = {"交大", "中大", "南大", "科大", "东大", "山大", "工大", "师大"}


def _school_record(name: str | None) -> dict | None:
    """先认全称，再认无歧义简称；院系/学生组织后缀不影响学校识别。"""
    if not name:
        return None
    name = name.strip()
    schools = _load_schools()
    # 优先匹配最长的机构全称，避免先命中另一个学校的短别名。
    matches = [s for s in schools if name.startswith(s["name"])]
    if matches:
        return max(matches, key=lambda s: len(s["name"]))
    aliases = [(a, s) for s in schools for a in s.get("aliases", [])
               if a and a not in _AMBIGUOUS_ALIASES and name.startswith(a)
               and (name == a or re.search(r"学院|学部|系|学生会|研究院|书院|校区", name[len(a):]))]
    if aliases:
        longest = max(len(a) for a, _ in aliases)
        matches = [s for a, s in aliases if len(a) == longest]
        return matches[0] if len({s.get("domain") for s in matches}) == 1 else None
    return None


def school_name(name: str | None) -> str:
    """规范学校名；未知中文学校保留本名供官网发现，不猜域名。"""
    record = _school_record(name)
    if record:
        return record["name"]
    value = (name or "").strip()
    match = re.match(r"^([^\s,，;；]{2,}?(?:大学|学院))", value)
    return match.group(1) if match else ""


def school_domain(name: str | None) -> str | None:
    record = _school_record(name)
    return record.get("domain") if record else None


def claim_school(claim: Claim) -> str:
    """只用本条陈述明确给出的学校，不把赛事主办方或其他经历的学校移植过来。"""
    entities = claim.entities or {}
    elements = dict(e.split("=", 1) for e in claim.elements or [] if "=" in e)
    values = [entities.get("school"), entities.get("university"), elements.get("学校"),
              entities.get("org"), *(elements.get(k) for k in ("任职组织", "机构", "授予单位", "单位"))]
    return next((s for v in values if isinstance(v, str) and (s := school_name(v))), "")


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

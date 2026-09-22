"""检索策略引擎：把「这条陈述该去哪搜」变成可扩展的数据，而不是 if-else。

分工：
  · `data/strategies.json`  领域知识（谁会公开什么文档）——加一种简历类型 = 加一个档案
  · 本文件                   解析、匹配、展开、排序、裁剪
  · 模型                     只负责读简历产出 profile（身份/行业/层级/技能），
                             不参与"够不够了""该不该再搜一条"的判断

设计要点见 docs/SEARCH_STRATEGY.md（工作区 search-strategy/DESIGN.md 同步一份）。

三条纪律：
  1. 查询必须挂在**要素**上——查不出任何要素的查询是浪费预算。
  2. 先定锚再取证（round 1 → 2 → 3），锚点定不住后面全是同名噪音。
  3. 识别不出身份类型就走 fallback，且 fallback 预算**更低**：
     不知道去哪找时，广撒网只会烧预算换噪音。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "strategies.json"

WECHAT_HOST = "mp.weixin.qq.com"

# 预算。**默认**（档案没指定时用）与**硬天花板**（任何档案都不能突破）是两回事：
# 差异化策略的意义就在于"该多搜的简历多搜"——金融/科研类中层以上任职确有公开权威记录，
# 值得多花预算；而识别不出身份时反而要**更少**预算（广撒网只换噪音）。
# 没有天花板的话，一个写错的档案就能把成本放大十倍。
MAX_SEARCHES = 6            # 默认；collect.Budget 也用它
MAX_PAGE_READS = 8
MAX_SECONDS = 90
CEILING_SEARCHES = 12       # 硬天花板
CEILING_PAGE_READS = 16
CEILING_SECONDS = 180


class StrategyError(RuntimeError):
    """策略档案读不出来或结构不对——响亮地失败，不要静默降级成空计划。"""


@dataclass
class Query:
    """一条检索查询。

    前三个字段是执行层要的；后面的是**可核查性**：一条查询必须能说出它在证哪个要素、
    为什么这么搜、预期拿到什么。没有这些，检索计划就退化成"撒网"。
    """

    text: str
    site: str | None = None
    kind: str = "web"                  # web / crossref / github / patent
    element: str | None = None         # 它在为哪个要素取证（"职务=部长"）
    purpose: str = ""                  # 这条查询想干什么
    round: int = 2                     # 1 定锚 / 2 取证 / 3 补漏
    weight: int = 0                    # 同轮内排序用，越大越先发
    source: str = ""                   # 来自哪个档案/类别，便于排查
    expect_tier: str | None = None     # 预期能拿到什么等级的来源
    variant: bool = False              # 扩展写法不挤掉原计划的检索渠道


@dataclass
class ResumeProfile:
    """模型读简历产出的画像。只影响"用哪套策略"，不影响任何判定。"""

    identity: str = "unknown"          # student / professional / researcher / creator / public_sector
    industries: list[str] = field(default_factory=list)
    level: str = "unknown"             # intern / entry / mid / senior / lead / exec
    skills: list[str] = field(default_factory=list)
    name: str = ""                     # 候选人姓名（简历原词）。报告署名与检索都用它

    @classmethod
    def from_dict(cls, d: dict | None) -> ResumeProfile:
        d = d or {}
        return cls(
            identity=(d.get("identity") or "unknown"),
            industries=list(d.get("industries") or []),
            level=(d.get("level") or "unknown"),
            skills=list(d.get("skills") or []),
            name=(d.get("name") or "").strip(),
        )


@dataclass
class Plan:
    queries: list[Query]
    profile_id: str
    profile_name: str
    notes: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)   # 因预算被裁掉的，如实记下来
    # 这个计划自己的预算。**执行层必须照它走**——档案说 8 次检索，
    # 而 collect 的默认预算还是 6 的话，多出来的两条查询永远不会被执行，策略就成了装饰。
    budget: dict = field(default_factory=dict)         # {searches, reads, seconds}


# ── 档案加载 ─────────────────────────────────────────────────────────────

_cache: dict | None = None


def load_strategies(path: Path | str | None = None, *, reload: bool = False) -> dict:
    global _cache
    if _cache is not None and path is None and not reload:
        return _cache
    p = Path(path) if path else DATA
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyError(f"读不出策略档案 {p}：{exc}") from exc
    if not isinstance(doc, dict) or "profiles" not in doc or "categories" not in doc:
        raise StrategyError(f"策略档案结构不对（需要 profiles 与 categories）：{p}")
    if path is None:
        _cache = doc
    return doc


# ── 档案匹配 ─────────────────────────────────────────────────────────────

def _hit(want: list, got: str | list) -> bool:
    if "*" in want:
        return True
    if isinstance(got, list):
        return bool(set(want) & set(got))
    return got in want


def _matches(rule: dict, prof: ResumeProfile, category: str, resume_cats: set[str]) -> bool:
    for key, want in rule.items():
        if key == "is_fallback":
            continue
        if key == "identity" and not _hit(want, prof.identity):
            return False
        if key == "industries" and not _hit(want, prof.industries):
            return False
        if key == "levels" and not _hit(want, prof.level):
            return False
        if key == "categories_any":
            # 对着**整份简历**的类别集合判，不是逐条陈述。
            # 它的本意是"这份简历像不像在校生"，而不是"这条陈述属不属于这些类别"——
            # 在校生简历里的论文/开源项目，照样该走在校生策略。当成逐条过滤器用会
            # 把它们踢到兜底档案（预算从 8 掉到 6），这是设计缺陷，不是精度提升。
            if "*" not in want and not (resume_cats & set(want)):
                return False
    return True


def match_profiles(doc: dict, prof: ResumeProfile, category: str,
                   resume_categories: list[str] | set[str] | None = None) -> list[dict]:
    """按具体度排序返回命中的档案。

    具体度 = 匹配条件里非通配的条目数（identity/industries/levels/categories 各算一条）。
    兜底档案**只在没有任何具体档案命中时**才用——否则它会拉低预算。
    """
    resume_cats = set(resume_categories) if resume_categories else {category}
    hits: list[tuple[int, dict]] = []
    fallback: dict | None = None
    for p in doc["profiles"]:
        rule = p.get("match") or {}
        if rule.get("is_fallback"):
            fallback = p
            continue
        if not _matches(rule, prof, category, resume_cats):
            continue
        score = 0
        for k in ("identity", "industries", "levels"):
            want = rule.get(k)
            if want and "*" not in want:
                score += 1
        if "categories_any" in rule and "*" not in rule["categories_any"]:
            score += 1
        hits.append((score, p))
    hits.sort(key=lambda t: -t[0])
    out = [p for _, p in hits]
    if not out and fallback is not None:
        out = [fallback]
    return out


# ── 占位符与域 ───────────────────────────────────────────────────────────

def _school_domain(org: str | None) -> str | None:
    """学校名 → 主域名。复用 plan.school_domain，避免两处口径不一致。"""
    from .plan import school_domain
    return school_domain(org)


_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def _from_elements(claim, key_names: list[str]) -> str:
    """从 elements（形如 "赛事=ICPC 亚洲区域赛"）里取值。

    为什么需要这条兜底：`entities` 是模型填的，不保证有；`elements` 是拆分步骤
    保证会有的。实测 c04（竞赛）的 entities 里没有 contest，于是模板展开出
    `"" 2023 获奖名单` 这种空引号查询——而 elements 里明明有 `赛事=ICPC 亚洲区域赛 晴川站`。
    """
    for el in claim.elements or []:
        if "=" not in el:
            continue
        k, _, v = el.partition("=")
        if k.strip() in key_names and v.strip():
            return v.strip()
    return ""


def _qualify(org: str, dept: str) -> str:
    """把"组织 + 部门"拼成全称。

    原版这里是 `dept or org`——当 dept 只是**部门**时（c05 的 org='晴川大学校学生会'、
    dept='科技部'），它会用"科技部"整个替掉组织名，搜出来的第一条查询是
    `"科技部" 换届 2024`，组织没了。拼全称才是对的：
    '晴川大学校学生会' + '科技部' → '晴川大学校学生会科技部'。

    中文组织名不加空格（"晴川大学计算机学院学生会技术部"），加了反而不像机构全称。
    """
    org, dept = (org or "").strip(), (dept or "").strip()
    if not dept:
        return org
    if not org or dept in org:
        return org or dept
    if org in dept:
        return dept
    return f"{org}{dept}"


def _paren_variants(value: str) -> list[str]:
    """从「全名（缩写）」里拆变体。**规则侧就能做，不必问模型。**

    '美国大学生数学建模竞赛（MCM）'   → ['美国大学生数学建模竞赛', 'MCM']
    '一等奖（Meritorious Winner）'    → ['一等奖', 'Meritorious Winner']

    为什么这很重要：公示名单里写的往往只有其中一种——官网列表常写全名，
    新闻报道常只写缩写。只拿全名去搜，等于主动放弃另一半可能。
    """
    v = (value or "").strip()
    if not v:
        return []
    out = [re.sub(r"[（(][^（）()]*[)）]", "", v).strip()]
    for m in re.findall(r"[（(]([^（）()]*)[)）]", v):
        s = m.strip()
        if s and s not in out:
            out.append(s)
    return [x for x in out if len(x) >= 2]


def _name_variants(value: str) -> list[str]:
    """把中英文混写姓名拆成搜索引擎更容易命中的独立写法。

    简历常写 ``林昱和 Leon Y. Lin``。把整个字符串放进一对引号，网页必须原样连续
    出现才会命中，召回率很低。这里保留中文名和英文名两把钥匙；只有单一语言时仍
    使用原词，不擅自交换英文姓与名。
    """
    value = (value or "").strip()
    if not value:
        return []
    chinese = "".join(re.findall(r"[\u3400-\u9fff]{2,}", value))
    latin_parts = re.findall(r"[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*)*", value)
    latin = " ".join(" ".join(latin_parts).split()).strip(" ./|")
    if chinese and latin:
        return list(dict.fromkeys([chinese, latin]))
    return [value]


def _context(claim, candidate_name: str, terms: list[str],
             emap: dict | None = None, profile: ResumeProfile | None = None) -> dict[str, str]:
    """占位符取值表。**entities 优先、elements 兜底**——
    entities 更具体（能区分 org 与 dept），elements 更可靠（一定存在）。"""
    e = claim.entities or {}
    emap = emap or {}

    def pick(field: str, *entity_keys: str) -> str:
        for k in entity_keys:
            v = (e.get(k) or "").strip() if isinstance(e.get(k), str) else ""
            if v:
                return v
        return _from_elements(claim, list(emap.get(field) or []))

    org = pick("org", "org")
    from .plan import claim_school
    school = claim_school(claim)
    dept = pick("dept", "dept")
    title = pick("title", "title")
    project = pick("project", "project") or title
    year = (claim.date_start or claim.date_label or "")[:4]
    nvar = _name_variants(candidate_name)

    # 变体钥匙：同一事物的不同写法各给一个占位符。
    # 「疑罪从有」的姿态就落在这里——搜不到时先换把钥匙，而不是收工。
    cvar = _paren_variants(pick("contest", "contest"))
    avar = _paren_variants(pick("award", "award"))
    tvar = _paren_variants(title)

    # 学年/期间：**用简历里写明的原词，不做任何换算或变体**。
    # "2025 学年校二等学术奖学金"——查询就该带着"2025 学年"这个原词；
    # 自己把 2025 换算成别的年份口径，只会搜出不相干的公示。
    m = re.search(r"(\d{4}\s*[-—–至]?\s*\d{0,4}\s*学年)", claim.raw_text or "")
    period = m.group(1).replace(" ", "") if m else (claim.date_label or "").strip()

    return {
        "name": nvar[0] if nvar else "",
        "name2": nvar[1] if len(nvar) > 1 else "",
        "period": period,
        "org": org,
        "school": school,
        "dept": dept,
        "role": pick("role", "role"),
        "orgname": _qualify(org, dept),         # 组织全称，学生工作/竞赛用它当主搜索词
        "company": pick("company", "company") or org,
        "major": pick("major", "major"),
        "contest": cvar[0] if cvar else pick("contest", "contest"),
        "contest2": cvar[1] if len(cvar) > 1 else "",
        "contest_v": " OR ".join(f'"{x}"' for x in cvar[:2]) if cvar else "",
        "award": avar[0] if avar else pick("award", "award"),
        "award2": avar[1] if len(avar) > 1 else "",
        "team": pick("team", "team"),
        "venue": pick("venue", "venue"),
        "paper": title,
        "title": tvar[0] if tvar else title,
        "title2": tvar[1] if len(tvar) > 1 else "",
        "project": project,
        "repo": pick("repo", "repo"),
        "year": year,
        "term": terms[0] if terms else "",
        "terms": " OR ".join(f'"{t}"' for t in terms[:3]) if terms else "",
    }


def _usable(tpl: str, ctx: dict[str, str]) -> tuple[bool, list[str]]:
    """这条模板现在能展开吗？缺哪些字段？

    展开前就判掉，而不是展开完再检查有没有 `""`。**没有值的占位符会让模板退化成
    空引号查询**（`"" 2023 获奖名单`）——那种查询发出去只会烧预算。
    """
    missing = [k for k in _PLACEHOLDER.findall(tpl) if not ctx.get(k)]
    return (not missing), missing


def _resolve_site(token: str | None, claim, ctx: dict[str, str] | None = None) -> str | None:
    """站点代号 → 真实域限定。

    `gov` / `platform` 这类**语义代号没有单一域可填**，退回不限定，靠查询词里的
    "公示/公告/年报"等词收敛。这是已知缺口，写在 notes 里，不假装做到了。
    """
    if not token:
        return None
    if token == "school":
        # 未知学校保留语义代号，交给执行层做一次动态域名发现。此前这里直接变成
        # None，collect.py 永远看不到 "school"，所谓动态发现实际上从未运行。
        org = (ctx or {}).get("school") or (ctx or {}).get("org") or (claim.entities or {}).get("org")
        return _school_domain(org) or "school"
    if token == "organizer":
        return (claim.entities or {}).get("organizer_domain") or None
    if token == "wechat":
        return WECHAT_HOST
    if token in ("gov", "platform"):
        return None
    return token


def _fill(tpl: str, ctx: dict[str, str]) -> str:
    """填占位符。未知占位符原样保留——宁可露出一处 {xxx} 让人发现，也不要静默填空。"""
    out = tpl
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", v)
    return " ".join(out.split())


def _targets(form: dict, claim) -> list[str]:
    """这条查询为哪些要素取证。

    默认 = 该陈述的**全部要素**（多数查询确实可能一次拿到多条）。
    只有确实只针对部分要素的表单才显式写 targets——否则等于给每条查询都贴一遍标签，
    噪声大于信息。`targets` 写的是要素的**键**（"职务"），引擎负责对上 `键=值`。
    """
    want = form.get("targets")
    if not want:
        return list(claim.elements or [])
    if isinstance(want, str):
        want = [want]
    out = []
    for w in want:
        for el in claim.elements or []:
            if el.split("=", 1)[0].strip() == w:
                out.append(el)
                break
    return out


# ── 展开成查询计划 ───────────────────────────────────────────────────────

def _search_exception(cat: dict, claim) -> bool:
    """never_search 类别的例外：命中 search_if_terms 里的词就恢复检索。

    用于「学历」里的保研/推免——普通学历不检索，但推免/保送有公开的推免公示、
    拟录取名单、夏令营优秀营员名单（多在学校官网），这类应当去查。判断只看
    简历原文与实体/要素里的原词，纯规则，不进模型。"""
    terms = cat.get("search_if_terms") or []
    if not terms:
        return False
    blob = claim.raw_text or ""
    for v in (claim.entities or {}).values():
        if isinstance(v, str):
            blob += " " + v
    blob += " " + " ".join(claim.elements or [])
    return any(t in blob for t in terms)


def build_plan(
    claim,
    candidate_name: str,
    profile: ResumeProfile | None = None,
    *,
    resume_categories: list[str] | set[str] | None = None,
    budget_searches: int | None = None,
    doc: dict | None = None,
) -> Plan:
    """把一条陈述 + 一份画像，展开成一个可执行的检索计划。

    `resume_categories` 是**整份简历**出现过的类别集合（流水线里拆分完就都有了）。
    档案匹配要用它，不能只看当前这一条的类别——见 `_matches` 里的说明。
    """
    doc = doc or load_strategies()
    prof = profile or ResumeProfile()

    cat = (doc["categories"].get(claim.category) or {})
    never = bool(cat.get("never_search"))
    exception = _search_exception(cat, claim) if never else False
    # never_search 类别：默认零检索；命中 search_if_terms 例外则放开全部表单；
    # 都不命中时，只放行标了 always 的**合规轻探针**（如学历的公众号「学校+姓名」粗检）。
    only_always = never and not exception
    _all_forms = (cat.get("official_forms") or []) + (cat.get("anchor_forms") or []) + (cat.get("forms") or [])
    if only_always and not any(f.get("always") for f in _all_forms):
        # 零预算是**有语义的**：这条陈述按设计不去检索。不能留空 dict——
        # 消费方（如 dsh 工具的输出 schema）要求 budget 三个字段都在。
        return Plan(
            [], "none", "不检索",
            notes=[f"{claim.category} 按设计不检索（学信网不做自动化）：直接 none + 授权补证"],
            budget={"searches": 0, "reads": 0, "seconds": 0},
        )

    profiles = match_profiles(doc, prof, claim.category, resume_categories)
    head = profiles[0] if profiles else {"id": "none", "name": "无匹配"}

    terms = list(cat.get("terms") or [])
    ctx = _context(claim, candidate_name, terms, doc.get("elements_map"))

    # 组装候选表单：类别锚点 → 类别取证 → 档案追加
    forms: list[tuple[dict, str]] = []
    # 官网先用最短的可用查询。学校已经由域名限定，不再重复完整院系组织名。
    # 每条陈述只加一条首选探针，空结果的渐进回退由 collect 在同一预算内处理。
    if ctx.get("school"):
        for f in cat.get("official_forms") or []:
            if only_always and not f.get("always"):
                continue
            if _usable(f.get("tpl", ""), ctx)[0]:
                forms.append((f, f"school:{claim.category}"))
                break
    for f in cat.get("anchor_forms") or []:
        if only_always and not f.get("always"):
            continue
        forms.append((f, f"category:{claim.category}"))
    for f in cat.get("forms") or []:
        if only_always and not f.get("always"):
            continue
        forms.append((f, f"category:{claim.category}"))
    for p in ([] if only_always else profiles):
        # 档案的追加查询只发给它**管得着**的类别。
        # categories_any 在这里是第二个用途：简历级匹配时它回答"这份简历像不像在校生"，
        # 到这里它回答"这条陈述值不值得用该档案的追加查询"。
        # 不加这一层，「互联网职场人」档案的 GitHub / 技术分享查询会套到「校内荣誉」头上，
        # 生成 `"林昱和" 晴川大学 技术 分享` 这种纯噪音。
        rule_cats = (p.get("match") or {}).get("categories_any") or ["*"]
        if "*" not in rule_cats and claim.category not in rule_cats:
            continue
        for f in p.get("extra_forms") or []:
            forms.append((f, f"profile:{p.get('id')}"))

    queries: list[Query] = []
    seen: set[tuple] = set()
    skipped: list[tuple[str, list[str]]] = []
    for form, source in forms:
        tpl = form.get("tpl", "")
        # 表单可以声明只对哪些类别有意义（如"学年评奖公示"只对荣誉/奖学金类）。
        # 没有这层限定，给学生工作发的查询里会混进"评奖公示"——它证不了职务，
        # 只是把预算烧在一个不可能出结果的查询上。
        only = form.get("categories")
        if only and claim.category not in only:
            continue
        ok, missing = _usable(tpl, ctx)
        if not ok:
            # 简历里没有这个字段。如实记下来——它说明"这条本来能搜，但缺数据"，
            # 和"搜了没找到"是两回事，不该混为一谈。
            skipped.append((tpl, missing))
            continue
        # 同一事实的中英文名/括号缩写分开检索。搜索供应商对复杂 OR 查询的支持并不
        # 稳定，两个短而精确的查询比一条长查询更可控；预算排序会保留最重要的变体。
        contexts = [ctx]
        variant_key = next((k for k in ("name", "contest", "award", "title")
                            if "{" + k + "}" in tpl and ctx.get(k + "2")), None)
        if variant_key:
            alt = dict(ctx)
            alt[variant_key] = ctx[variant_key + "2"]
            contexts.append(alt)

        for variant_index, variant_ctx in enumerate(contexts):
            text = _fill(tpl, variant_ctx)
            if not text:
                skipped.append((tpl, ["（展开后为空）"]))
                continue
            site = _resolve_site(form.get("site"), claim, variant_ctx)
            # 「没有域就别发」：有的模板（如赛事组委会的 `{year} 获奖`）本身几乎不含信息，
            # 精确性**完全靠 site 限定**。域拿不到时它退化成"2023 获奖"这种烂查询，
            # 发出去只会烧预算。动态学校域用 "school" 代号，仍算可解析的域。
            if form.get("requires_site") and not site:
                skipped.append((tpl, [str(form.get("site")) + " 域名"]))
                continue
            kind = form.get("kind") or "web"
            # GitHub 检索只对真正的仓库路径（user/repo）有意义。简历里常见"只有项目名没有
            # 仓库"，那种情况该走 web 的 {project} 查询——把中文项目名塞给 GitHub 搜索
            # 只会得到 0 结果，还白花一次检索。
            if kind == "github" and ("/" not in text or " " in text.strip('"')):
                kind = "web"
            key = (text, site, kind)
            if key in seen:
                continue
            seen.add(key)
            els = _targets(form, claim)
            queries.append(Query(
                text=text, site=site, kind=kind,
                element=els[0] if len(els) == 1 else None,
                purpose=form.get("purpose", ""),
                round=int(form.get("round") or 2),
                weight=int(form.get("weight") or 0) - variant_index * 3,
                source=source,
                expect_tier=form.get("expect_tier"),
                variant=bool(variant_index),
            ))

    # 预算：档案说了算，但被硬天花板夹住；collect 的默认值只作兜底
    pb = head.get("budget") or {}
    budget = {
        "searches": int(pb.get("searches") or MAX_SEARCHES),
        "reads": int(pb.get("reads") or MAX_PAGE_READS),
        "seconds": float(pb.get("seconds") or MAX_SECONDS),
    }
    if budget_searches is not None:
        budget["searches"] = min(budget["searches"], int(budget_searches))
    budget["searches"] = max(1, min(budget["searches"], CEILING_SEARCHES))
    budget["reads"] = max(1, min(budget["reads"], CEILING_PAGE_READS))
    budget["seconds"] = max(5.0, min(budget["seconds"], CEILING_SECONDS))

    # 先保留各原始渠道，再追加别名变体；同组内按轮次、权重、域限定排序。
    queries.sort(key=lambda q: (q.variant, not q.source.startswith("school:"),
                                q.round, -q.weight, 0 if q.site else 1, q.text))

    cap = budget["searches"]
    dropped = [q.text for q in queries[cap:]]
    plan = Plan(queries[:cap], head.get("id", "none"), head.get("name", "无匹配"),
                notes=list(head.get("notes") or []), dropped=dropped, budget=budget)

    if skipped:
        fields: list[str] = []
        for _, miss in skipped:
            for m in miss:
                if m not in fields:
                    fields.append(m)
        plan.notes.append(
            f"有 {len(skipped)} 条查询模板因简历缺字段没展开（缺：{'、'.join(fields[:4])}）"
            "——这是「搜不了」，不是「搜了没找到」。")
    if not plan.queries:
        plan.notes.append("这条陈述没有产出任何可用查询——检查画像与档案是否对得上。")
    if any(str(f.get("site") or "") in ("gov", "platform") for f, _ in forms):
        plan.notes.append(
            "gov / platform 这类来源没有单一域可填 site，只能靠查询词收敛，命中率低于域限定。")
    return plan


def plan_for_claim(claim, candidate_name: str, profile: ResumeProfile | None = None) -> list[Query]:
    """兼容入口：只要查询列表。"""
    return build_plan(claim, candidate_name, profile).queries

"""自动发现学校官网的通知栏目（给 app/archive.py 的按日期定位用）。

人工给 400 多所学校逐个配栏目不现实，这里按固定套路自动找：

  1. 学校首页里文字含「本科生院 / 教务处」（推荐方）或「研究生院 / 研究生招生」（接收方）
     的链接，加上常见子域名猜测（jwc. / ugs. / yz. / yzb. / gs. …）；
  2. 在这些站点首页里找「工作通知 / 通知公告 / 招生通知 …」栏目；
  3. 栏目页必须能读出至少 5 条带日期的通知，并能从"下一页 / 尾页"推出翻页规律
     （WebPlus 递增、博达 VSB 递减两种都认），第 2 页的日期必须比第 1 页旧——
     三条都满足才算发现成功。

结果写进 `data/archive_cache.json`：成功的缓存 180 天，失败的缓存 30 天（避免每次
核验都重新试一遍不存在的站点）。`schools.json` 里人工写的 `notice_archives`
永远优先于自动发现。全程用调用方的 PageFetcher：守 robots、单域限流、短超时。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .archive import _same_page, pagination, parse_list_page

CACHE_PATH = Path(os.environ.get("ARCHIVE_CACHE") or
                  Path(__file__).resolve().parent.parent / "data" / "archive_cache.json")
OK_DAYS, FAIL_DAYS = 180, 30

PREFIXES = {
    "source": ["jwc", "jwch", "ugs", "jwb", "jw", "bksy", "aao", "jiaowu", "dean", "undergrad",
               "bkjx", "jxb", "jwzx", "bks", "jwgl"],
    "receiver": ["yz", "yzb", "yzw", "gs", "grs", "graduate", "yjsy", "yjszs", "yjs", "gra",
                 "yzc", "gsao", "yjsc", "yzbw"],
}
OFFICE_WORDS = {
    "source": ("本科生院", "教务处", "教务部", "本科教学", "本科生教育", "本科教育", "教务在线",
               "教学事务部", "教务办"),
    "receiver": ("研究生招生", "研招", "研究生院", "研究生教育", "研究生处", "研究生工作部",
                 "研究生部"),
}
# 栏目名称（越靠前越优先）。推荐方把「推免」专栏放最前面：上海大学本科生院就有「推免研究生」。
COLUMN_WORDS = {
    "source": ("推免", "推荐免试", "免试研究生", "工作通知", "教务通知", "通知公告", "通知通告",
               "最新通知", "公告通知", "教学通知", "公示公告", "通知", "公告", "公示", "教务动态",
               "教学动态", "工作动态"),
    "receiver": ("推免", "推荐免试", "硕士招生", "招生通知", "招生公告", "招生信息", "招生动态",
                 "招生工作", "招生资讯", "招生管理", "通知公告", "最新通知", "通知", "公告", "公示"),
}
# 栏目地址里的拼音缩写：标题是图片、或"更多"链接旁边认不出标题时，按地址认。
COLUMN_SLUGS = {
    "source": (("tmyjs", 0), ("tm", 1), ("tjms", 1), ("tzgg", 5), ("tztg", 5), ("ggtz", 5),
               ("zxtz", 6), ("tzgs", 6), ("jwtz", 4), ("gztz", 3), ("gsgg", 10), ("tz", 12), ("gg", 13)),
    "receiver": (("tmzs", 0), ("tm", 1), ("tjms", 1), ("sszs", 2), ("zstz", 3), ("zsgg", 3), ("zsxx", 4),
                 ("zsdt", 5), ("zsgz", 6), ("tzgg", 10), ("zxtz", 11), ("tz", 12), ("gg", 13)),
}
DEFAULT_WINDOW = {"source": ([8, 11], -1), "receiver": ([9, 12], -1)}

_lock = threading.Lock()
# 批量发现时可设成一个目录：没通过的候选页存一份 HTML，方便人工对着改规则。
DEBUG_DIR: Path | None = None


def _dump(school: str, role: str, url: str, html: str | None, note: str) -> None:
    if DEBUG_DIR is None or not html:
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", f"{school}_{role}_{urlparse(url).netloc}{urlparse(url).path}")[:150]
        (DEBUG_DIR / f"{safe}.html").write_text(
            f"<!-- url: {url} -->\n<!-- note: {note} -->\n" + html[:300_000], encoding="utf-8")
    except OSError:
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_cache(path: Path = CACHE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"_note": "自动发现的学校通知栏目缓存，见 app/discover.py；可删除，删除后会重新发现。",
                "schools": {}}


def save_entry(school: str, role: str, entry: dict, path: Path = CACHE_PATH) -> None:
    with _lock:
        data = load_cache(path)
        data.setdefault("schools", {}).setdefault(school, {})[role] = entry
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)


def cached(school: str, role: str, path: Path = CACHE_PATH) -> dict | None:
    """有效期内的缓存条目；过期或没有返回 None。"""
    entry = load_cache(path).get("schools", {}).get(school, {}).get(role)
    if not entry:
        return None
    try:
        checked = datetime.fromisoformat(entry["checked_at"])
    except Exception:
        return None
    days = OK_DAYS if (entry.get("archives") or entry.get("colleges")) else FAIL_DAYS
    return entry if _now() - checked < timedelta(days=days) else None


def _in_domain(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith("." + domain)


def _anchors(html: str, base: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript", "#", "mailto")) or "<" in href:
            continue
        text = re.sub(r"\s+", "", a.get_text("", strip=True) or a.get("title", ""))
        yield urljoin(base, href), text


def _is_more(text: str) -> bool:
    from .archive import MORE_RE
    t = unicodedata.normalize("NFKC", re.sub(r"\s+", "", text or ""))
    return bool(t) and bool(MORE_RE.match(t))


def _cn(text: str) -> str:
    """去掉英文副标题和分隔符：「通知公告 Announcements」→「通知公告」。"""
    return re.sub(r"[A-Za-z\s/|·•\-_&.:：]+", "", text or "")


def _heading_for(a) -> str:
    """"更多>>" 链接所在版块的标题：往上找祖先节点，取链接前面那段短文字。

    大量教务处/研究生院首页的栏目标题不是链接，只有右上角"更多"能点
    （中财教务处「通知公告 更多>>」、哈工大本科生院「通知公告 更多>」、闽南师大「通知公告/Annc 更多>>」、
    东华教务处「通知公告 Announcements Ｍore+」）。选项卡式版块（华南农大「教务公告｜招生专栏」
    后面跟两个"更多+"）按位置一一对应。
    """
    from bs4 import NavigableString
    node = a
    for _ in range(4):
        node = node.parent
        if node is None:
            return ""
        before: list[str] = []
        after: list[str] = []
        inside = set(id(x) for x in a.descendants)
        mores = [x for x in node.select("a[href]") if _is_more(x.get_text("", strip=True))]
        seen_a = False
        for piece in node.descendants:
            if piece is a:
                seen_a = True
                continue
            if isinstance(piece, NavigableString) and id(piece) not in inside \
                    and piece.parent is not None and piece.parent.name not in ("script", "style"):
                t = _cn(str(piece))
                if t and not _is_more(str(piece)):
                    (after if seen_a else before).append(t)
        if len(mores) > 1 and a in mores and len(before) == len(mores) \
                and all(2 <= len(t) <= 8 for t in before):
            return before[mores.index(a)]          # 选项卡标题与"更多"按顺序对应
        text = "".join(before)
        if 2 <= len(text) <= 16:
            return text
        if not before and after and 2 <= len("".join(after)) <= 16:
            return "".join(after)          # "更多"写在标题前面的版块
        if len(text) > 16:
            last = before[-1] if before else ""
            return last if 2 <= len(last) <= 12 else ""
    return ""


def _labeled(html: str, base: str):
    """(网址, 栏目名)：普通链接用链接文字，"更多"类链接用所在版块的标题。"""
    from bs4 import BeautifulSoup
    from .archive import MORE_RE
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript", "#", "mailto")) or "<" in href:
            continue
        text = unicodedata.normalize("NFKC", re.sub(r"\s+", "", a.get_text("", strip=True) or a.get("title", "")))
        img = a.find("img")
        more_img = img is not None and re.search(r"more|更多|gd\.|jt", f"{img.get('alt', '')} {img.get('src', '')}", re.I)
        if (text and MORE_RE.match(text)) or (not text and more_img):
            # 认不出标题也照样给出来（标题为空），让栏目地址的拼音缩写（tztg、tzgg…）还能排上。
            yield urljoin(base, href), _heading_for(a), True
            continue
        yield urljoin(base, href), text, False


# 只用字面网址的跳转页（南大本科生院 location.href="//jw.nju.edu.cn"、
# 西北大学研招 location.href="https://yjs.nwu.edu.cn/…"、<meta http-equiv=refresh>）。
_REDIRECT_RES = (
    re.compile(r"""http-equiv=["']?refresh["']?[^>]*?url\s*=\s*['"]?([^'">\s]+)""", re.I),
    re.compile(r"""(?:window\.|self\.|top\.)?location(?:\.href)?\s*=\s*["']([^"'+]+)["']\s*;?\s*(?:<|$|\n)""", re.I),
    re.compile(r"""location\.(?:replace|assign)\(\s*["']([^"'+]+)["']\s*\)""", re.I),
)


def redirect_target(html: str, base: str) -> str | None:
    """很短的跳转页里写死的目标地址；拼接出来的地址（登录系统常见）不跟。"""
    if not html or len(html) > 6000:
        return None
    for rx in _REDIRECT_RES:
        m = rx.search(html)
        if m:
            target = urljoin(base, m.group(1).strip())
            if target.startswith(("http://", "https://")) and target.rstrip("/") != base.rstrip("/"):
                return target
    return None


# 教务管理系统、研究生报名系统之类的登录页：不是通知网站，别占用候选名额。
SYSTEM_TITLE_RE = re.compile(r"登录|登入|管理系统|管理信息系统|服务系统|服务平台|报名系统|信息系统|统一身份|认证")


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def _homepage(domain: str, fetch) -> tuple[str, str] | None:
    for url in (f"https://www.{domain}/", f"http://www.{domain}/", f"https://{domain}/",
                f"http://{domain}/"):
        html = fetch(url)
        if html:
            return url, html
    return None


WAF_RE = re.compile(r"防火墙|WAF|安全狗|访问被拦截|访问受限|请开启JavaScript|请启用JavaScript", re.I)


def _school_names(school: str) -> list[str]:
    names = [re.sub(r"[（(].*?[）)]", "", school)]
    try:
        from .plan import _school_record
        record = _school_record(school) or {}
        names += [re.sub(r"[（(].*?[）)]", "", a) for a in record.get("aliases") or [] if len(a) >= 3]
    except Exception:
        pass
    return [n for n in dict.fromkeys(names) if n]


def check_domain(school: str, domain: str, fetch, failures: dict | None = None) -> dict:
    """首页能打开、<title> 里出现学校名（或表里的别名，如皖南医学院→皖南医科大学）才算域名可信。

    首页是字面跳转页（meta refresh / location.href="…"）的跟一次；网站防火墙
    （HTTP 412、"WEB应用防火墙"拦截页）只记原因，不绕过。
    """
    home = _homepage(domain, fetch)
    if not home:
        why = (failures or {}).get(f"https://www.{domain}/") or (failures or {}).get(f"http://{domain}/")
        if why and ("412" in why or "防火墙" in why):
            why = f"{why}，网站防火墙要求浏览器执行脚本，程序不绕过"
        return {"ok": False, "reason": "首页打不开" + (f"（{why}）" if why else "")}
    url, html = home
    target = redirect_target(html, url)
    if target and _in_domain(target, domain):
        followed = fetch(target)
        if followed:
            url, html = target, followed
    title = _title(html)
    names = _school_names(school)
    hit = any(n in title or n in html[:20000] for n in names)
    reason = ""
    if not hit:
        reason = ("网站防火墙拦截页（程序不绕过）" if WAF_RE.search(title + html[:3000])
                  else "首页没有出现学校名")
    return {"ok": hit, "url": url, "title": title[:60], "reason": reason}


ORG_WORDS = ("机构设置", "组织机构", "管理机构", "职能部门", "党政部门", "机构导航", "部门导航",
             "党政机构", "管理部门", "部门网站", "党群部门", "行政部门")


def _office_links(html: str, base: str, domain: str, role: str) -> list[str]:
    out: list[str] = []
    for url, text in _anchors(html, base):
        if _in_domain(url, domain) and any(w in text for w in OFFICE_WORDS[role]) and len(text) <= 14 \
                and not SYSTEM_TITLE_RE.search(text) and url not in out:
            out.append(url)
    return out


def _offices(domain: str, role: str, fetch, home: tuple[str, str] | None) -> list[str]:
    """候选站点：首页里的链接 > 「机构设置」页里的链接 > 常见子域名猜测。

    天大 oaa.、哈工大 hituc.、南科大 tao.、北化 jiaowuchu. 这类非常规子域只能从链接里找到；
    首页没有直接链接的，再看一次「机构设置/职能部门」页。
    """
    found: list[str] = []
    if home:
        found = _office_links(home[1], home[0], domain, role)
        if not found:
            org = next((u for u, t in _anchors(home[1], home[0])
                        if _in_domain(u, domain) and t in ORG_WORDS), None)
            org_html = fetch(org) if org else None
            if org_html:
                found = _office_links(org_html, org, domain, role)
    for prefix in PREFIXES[role]:
        url = f"https://{prefix}.{domain}/"
        if url not in found:
            found.append(url)
    return found


ROLE_SITE_WORDS = {"source": ("教务", "本科", "教学"), "receiver": ("研究生", "研招", "招生")}


NOT_OFFICE_RE = re.compile(r"纪检|纪委|监察|审计|保卫|后勤|财务|工会|宣传|组织部|统战|离退休|图书馆|档案")


def _office_matches(html: str, role: str) -> bool:
    """站点名称（<title>）或页头文字里有"教务/本科/教学"（研究生方："研究生/研招/招生"）。

    页头只看去掉脚本样式后的前 600 个字：不少处室网站 <title> 只写"首页"，名称在 logo 文字里；
    但纪检监察网等页脚里也会有"教务处"链接，所以不看整页，<title> 是纪检、后勤等的一律不算。
    """
    title = _title(html)
    if NOT_OFFICE_RE.search(title):
        return False
    if any(w in title for w in ROLE_SITE_WORDS[role]):
        return True
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", (html or "")[:60000], flags=re.S | re.I)
    head = re.sub(r"\s+", "", re.sub(r"<[^>]+>", " ", body))[:600]
    alts = re.findall(r'<img[^>]+alt=["\']([^"\']{2,30})', body[:20000], re.I)
    return any(w in head + "".join(alts) for w in ROLE_SITE_WORDS[role])


def _slug_rank(url: str, role: str) -> int | None:
    path = urlparse(url).path.lower()
    segs = [re.sub(r"\.(s?html?|jsp|php|aspx?)$", "", x) for x in path.split("/") if x]
    for seg in reversed(segs[-2:]):
        seg = re.sub(r"\d+$", "", seg)
        for slug, rank in COLUMN_SLUGS[role]:
            if seg == slug:
                return rank
    return None


# 具体文章的地址（博达 info/1012/5283.htm、WebPlus 2026/0911/c445a62076/page.htm、content_101395.html），
# 不是栏目。注意 info/iList.jsp?cat_id=… 是栏目（西北政法教务处）。
ARTICLE_RE = re.compile(r"/info/\d+/\d+\.s?html?|/c\d+a\d+/|/page\.htm|/content[_/]\d+|/\d{4}/\d{4}/|/art/\d{4}/|"
                        r"[?&](wbnewsid|newsid|articleid|id)=\d+")


# 名字里带"公示/通知"但不是我们要的栏目（北语「教育收费公示」、「招标公告」、「人才招聘」…）
NOT_COLUMN_RE = re.compile(r"收费|招标|采购|招聘|党建|党务|工会|图片|视频|下载|规章|制度|政策法规|继续教育|"
                           r"校友|就业|资助|讲座|媒体|新闻|成人|网络教育|留学生|国际学生|博士后")


def _columns(html: str, base: str, domain: str, role: str) -> list[str]:
    """站点首页里的通知类栏目，按栏目名排序；"更多>>"链接按所在版块标题认，
    标题认不出的再按地址里的拼音缩写（tzgg、zxtz、sszs、tmyjs…）认。"""
    ranked: list[tuple[int, str]] = []
    words = COLUMN_WORDS[role]
    for url, text, via_more in _labeled(html, base):
        if not _in_domain(url, domain) or len(text) > (16 if via_more else 10):
            continue
        if SYSTEM_TITLE_RE.search(text) or ARTICLE_RE.search(url):
            continue                    # 具体文章、登录系统不是栏目
        slug = _slug_rank(url, role)
        rank = next((i for i, w in enumerate(words) if w in text), None)
        if rank is not None and not NOT_COLUMN_RE.search(text):
            ranked.append((rank * 10 + (0 if text == words[rank] else 3) + (1 if via_more else 0), url))
            continue
        if slug is not None and (via_more or len(text) <= 6 or rank is not None):
            ranked.append((200 + slug * 10, url))
    out: list[str] = []
    for _, url in sorted(ranked):
        if url not in out and not url.rstrip("/").endswith(urlparse(base).netloc):
            out.append(url)
    return out


def _is_push_column(url: str, label: str) -> bool:
    return any(w in label for w in ("推免", "推荐免试", "免试")) or _slug_rank(url, "source") in (0, 1)


def _has_live_next(html: str, url: str = "") -> bool:
    """页面上有可点的"下一页"（不是 javascript:void、灰掉的 span、也不是指回本页）。"""
    from bs4 import BeautifulSoup
    from .archive import _same_page
    for a in BeautifulSoup(html or "", "html.parser").select("a[href]"):
        href = a.get("href", "").strip()
        if any(w in a.get_text("", strip=True) for w in ("下一页", "下页")) \
                and href and not href.startswith(("javascript", "#")) \
                and not (url and _same_page(urljoin(url, href), url)):
            return True
    return False


def _validate_column(url: str, html: str, fetch) -> tuple[dict | None, str]:
    items, _ = parse_list_page(html, url)
    if 3 <= len(items) < 5 and not pagination(html, url) and not _has_live_next(html, url) \
            and any(w in i.title for i in items for w in ("推免", "免试", "推荐免试")):
        # 推免专栏常常只有三四条（西安石油「推免生」、南航「推荐免试」），一页就是全部。
        return ({"first_page": url, "list_url": url, "page_order": "single", "total_pages_seen": 1,
                 "newest": max(i.date for i in items), "oldest_seen": min(i.date for i in items)}, "")
    if len(items) < 5:
        if len(html or "") < 3000 or "<a" not in (html or "")[:200000]:
            return None, f"带日期的条目只有 {len(items)} 条（页面基本是空的，可能要浏览器执行脚本才出列表）"
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I))
        if len(re.findall(r"(?<![\d\-/.])[01]?\d[-/.][0-3]\d(?![\d\-/.])|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|"
                          r"Aug|Sep|Oct|Nov|Dec)\b", text)) >= 5:
            return None, f"带日期的条目只有 {len(items)} 条（列表只写月日、没写年份，没法按年份定位）"
        return None, f"带日期的条目只有 {len(items)} 条"
    pag = pagination(html, url)
    if not pag:
        if not _has_live_next(html, url):
            # 条目不多、只有一页的栏目（研招网的"最新公告"常见）也能用。
            return ({"first_page": url, "list_url": url, "page_order": "single", "total_pages_seen": 1,
                     "newest": max(i.date for i in items), "oldest_seen": min(i.date for i in items)}, "")
        return None, "推不出翻页规律（没有可用的下一页/尾页链接）"
    archive = {"first_page": url, "list_url": pag["template"], "page_order": pag["order"],
               "page_offset": pag.get("offset", 0)}
    total = pag.get("total")
    if pag["order"] == "reverse" and not total:
        return None, "倒序翻页但拿不到总页数"
    second = archive["list_url"].format(
        page=(total - 1) if pag["order"] == "reverse" else 2 + pag.get("offset", 0))
    html2 = fetch(second)
    if not html2:
        return None, f"第 2 页读不到：{second}"
    items2, _ = parse_list_page(html2, second)
    if len(items2) < 3:
        return None, f"第 2 页带日期的条目只有 {len(items2)} 条：{second}"
    newest2 = max(i.date for i in items2)
    oldest1 = min(i.date for i in items)
    if newest2 > max(i.date for i in items):          # 第 2 页比第 1 页还新：规律推错了
        return None, "第 2 页比第 1 页还新，翻页规律推错"
    archive.update({"total_pages_seen": total, "newest": max(i.date for i in items),
                    "oldest_seen": min(oldest1, min(i.date for i in items2))})
    return archive, ""


def discover(school: str, domain: str, role: str, fetch, *, deadline: float | None = None,
             max_fetches: int = 30, say=lambda m: None, why=None) -> dict:
    """返回缓存条目：{"archives": [...], "checked_at", "tried", "reason"}。不写缓存。

    `why(url)` 可选，返回读页失败的原因（PageFetcher.failures.get），写进 unreachable 统计，
    方便分清是"没有这个子站"还是"连不上/被拦"。
    """
    used = 0
    tried: list[str] = []
    counter = threading.Lock()

    def get(url: str) -> str | None:
        nonlocal used
        with counter:
            if used >= max_fetches or (deadline is not None and time.monotonic() >= deadline):
                return None
            used += 1
        return fetch(url)

    rejects: list[dict] = []
    unreachable: dict[str, int] = {}
    entry = {"checked_at": _now().isoformat(timespec="seconds"), "archives": [], "tried": tried,
             "domain": domain, "rejects": rejects, "office_checked": True}
    if not domain:
        entry["reason"] = "没有学校域名"
        return entry
    home = _homepage(domain, get)
    offices = _offices(domain, role, get, home)
    guessed = {f"https://{p}.{domain}/" for p in PREFIXES[role]}
    linked = {o for o in offices if o not in guessed}
    # 各候选站点是不同的主机，并行探测（同一主机仍由 PageFetcher 串行限流）。
    # 绝大多数猜测的子域根本不存在，串行时每个都要等满连接超时。
    from concurrent.futures import ThreadPoolExecutor

    def probe(url: str) -> str | None:
        html = get(url)
        if html is None and url.startswith("https://") and why and "SSL" in (why(url) or ""):
            # 不少学院/处室子站只开了 http，https 端口落到学校统一网关、证书对不上域名
            # （厦大 jwc.xmu.edu.cn 就是 http）。证书不对时退回 http 再读一次。
            alt = "http://" + url[len("https://"):]
            html = get(alt)
            if html is not None:
                moved[url] = alt
        return html

    moved: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        office_pages = dict(zip(offices, pool.map(probe, offices)))
    checked_pages: list[str] = []
    queue = list(offices)
    hubs = 0
    while queue:
        office = queue.pop(0)
        if deadline is not None and time.monotonic() >= deadline:
            break
        html = office_pages[office] if office in office_pages else probe(office)
        tried.append(office)
        office = moved.get(office, office)
        if html:
            target = redirect_target(html, office)
            if target and _in_domain(target, domain):
                html2 = get(target)
                if html2:
                    office, html = target, html2
                    tried.append(target)
        if not html:
            reason = (why(office) if why else "") or "读不到"
            # 猜出来的子域名大多根本不存在：DNS 查不到（连接失败），或者被学校的泛域名解析
            # 指到统一网关、证书对不上（SSL）。这两种不算"连不上"，只统计真正可疑的原因。
            if office in linked or not (reason.startswith("连接失败") or "SSL" in reason):
                unreachable[reason] = unreachable.get(reason, 0) + 1
                if office in linked:
                    rejects.append({"url": office, "reason": f"站点打不开（{reason}）"})
            continue
        host = urlparse(office).hostname or ""
        if any(_same_page(office, seen) for seen in checked_pages):
            continue                       # http/https、带不带 index 的同一个页面只看一次
        checked_pages.append(office)
        if SYSTEM_TITLE_RE.search(_title(html)):
            rejects.append({"url": office, "reason": f"是登录/管理系统（{_title(html)[:20]}），不是通知网站"})
            continue
        if not _office_matches(html, role):
            # 主站上的「人才培养」页（武大 www.whu.edu.cn/rcpy.htm）名字不像教务处，但链着本科生院：
            # 从首页链接来的页面，名字不对也先看看有没有指向处室子站的链接。
            extra = [u for u in _office_links(html, office, domain, role)
                     if urlparse(u).hostname != host and u not in offices and u not in queue] \
                if office in linked else []
            if extra and hubs < 2:
                hubs += 1
                queue[0:0] = extra[:3]
                linked.update(extra[:3])
                rejects.append({"url": office, "reason": f"不是处室网站，转到 {', '.join(extra[:3])}"})
                continue
            # jwb. 在不少学校是纪检监察办，不是教务部；猜出来的子域要核对站点名称。
            rejects.append({"url": office, "reason": "站点名称不像" + ("教务/本科" if role == "source" else "研究生/研招")
                            + (f"（{_title(html)[:20]}）" if _title(html) else "")})
            if office in linked:
                _dump(school, role, office, html, "站点名称不像")
            continue
        columns = _columns(html, office, domain, role)[:4]
        if not columns:
            # 主站上的「本科生教育 / 研究生教育」介绍页（清华、北工大、华南师大…）本身没有通知栏目，
            # 但会链到真正的本科生院、研招网子站：把这些子站接到队列里再看。
            extra = [u for u in _office_links(html, office, domain, role)
                     if urlparse(u).hostname != host and u not in offices and u not in queue]
            if extra and hubs < 2:
                hubs += 1
                queue[0:0] = extra[:3]
                linked.update(extra[:3])
                rejects.append({"url": office, "reason": f"介绍页，转到 {', '.join(extra[:3])}"})
                continue
            rejects.append({"url": office, "reason": "站点首页没有匹配的栏目链接"})
            _dump(school, role, office, html, "站点首页没有匹配的栏目链接")
        labels = {u: t for u, t, _ in _labeled(html, office)}
        sites = [c for c in columns if urlparse(c).path in ("", "/") and urlparse(c).hostname != host]
        for site_url in sites:
            # 研究生院首页上的「招生工作」常常直接链到研招网子站的首页：当成候选站点排进队列。
            if site_url not in offices and site_url not in queue:
                queue.insert(0, site_url)
                linked.add(site_url)
        columns = [c for c in columns if c not in sites]
        for column in columns:
            if entry["archives"] and not (role == "source" and _is_push_column(column, labels.get(column, ""))):
                continue                   # 找到一个以后，只再补推免专栏
            col_html = get(column)
            tried.append(column)
            if not col_html:
                rejects.append({"url": column, "reason": "栏目页读不到" + (f"（{why(column)}）" if why and why(column) else "")})
                continue
            archive, reason = _validate_column(column, col_html, get)
            if not archive:
                rejects.append({"url": column, "reason": reason})
                _dump(school, role, column, col_html, reason)
                continue
            months, offset = DEFAULT_WINDOW[role]
            archive.update({"name": f"{school}·{labels.get(column) or '通知栏目'}", "role": role,
                            "months": months, "year_offset": offset, "source": "auto"})
            entry["archives"].append(archive)
            say(f"    栏目发现：{school} {role} → {column}（{archive['page_order']}）")
            if len(entry["archives"]) >= 2:
                break
        if entry["archives"]:
            entry["fetches"] = used
            return entry
    if unreachable:
        entry["unreachable"] = unreachable
    if deadline is not None and time.monotonic() >= deadline:
        entry["reason"] = "时间用完"             # 不缓存：下次核验接着试
    elif used >= max_fetches:
        entry["reason"] = "读页次数用完"
    elif not any(office_pages.values()):
        entry["reason"] = "候选站点都打不开" + (
            f"（{'、'.join(f'{k}×{v}' for k, v in sorted(unreachable.items(), key=lambda kv: -kv[1])[:3])}）"
            if unreachable else "")
    else:
        entry["reason"] = "没找到可翻页的通知栏目"
    entry["fetches"] = used
    return entry


def archives_for(school: str, role: str, fetch, *, domain: str | None = None,
                 deadline: float | None = None, allow_discovery: bool | None = None,
                 say=lambda m: None, path: Path = CACHE_PATH) -> list[dict]:
    """人工配置 > 有效缓存 > 现场发现（并写缓存）。"""
    from .plan import school_archives, school_domain, school_name
    configured = school_archives(school, role)
    if configured:
        return configured
    name = school_name(school) or school
    hit = cached(name, role, path)
    if hit is not None:
        return hit.get("archives") or []
    domain = domain or school_domain(school)
    if allow_discovery is None:
        allow_discovery = os.environ.get("ARCHIVE_DISCOVERY", "on") != "off"
    if not allow_discovery or not domain:
        return []
    entry = discover(name, domain, role, fetch, deadline=deadline, say=say)
    if entry["archives"] or entry.get("reason") != "时间用完":
        try:
            save_entry(name, role, entry, path)
        except OSError:
            pass
    if not entry["archives"]:
        say(f"    栏目发现：{name} {role} 未找到（{entry.get('reason')}），30 天内不再重试")
    return entry["archives"]


# ── 学院层：推免名单发在各学院网站的学校（如华南理工，教务处又只对校内开放）──────────

COLLEGE_INDEX_WORDS = ("院系设置", "学院设置", "机构设置", "院系导航", "教学单位", "学院导航",
                       "教学科研单位", "院系部门", "院部设置", "院系")
COLLEGE_COLUMN_WORDS = {
    # 推荐方：本科生/教务类栏目（华工计算机学院「教务通知」里有拟推荐名单）
    "source": ("本科生通知", "本科教学", "教务通知", "本科教务", "教学通知", "本科生教育",
               "通知公告", "公示公告", "公告通知", "学院通知", "通知"),
    # 接收方：招生类栏目（华工工商管理学院「招生资讯」里有推免复试拟录取名单）
    "receiver": ("招生资讯", "研究生招生", "招生信息", "硕士招生", "招生工作", "研究生通知",
                 "通知公告", "公示公告", "通知"),
}
_GENERIC = re.compile(r"学院|学部|研究院|系|科学|工程|技术|与|及|和|专业|大学|学科|类")


def _core(text: str) -> str:
    return _GENERIC.sub("", re.sub(r"[（(].*?[）)]|\s", "", text or ""))


def _common(a: str, b: str) -> int:
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def match_college(colleges: list[list[str]], hint: str) -> list[str] | None:
    """按简历里的本科学院/专业挑学院：去掉"学院/科学/工程"等通用字后，最长公共片段 ≥2 且唯一最优。"""
    want = _core(hint)
    if len(want) < 2:
        return None
    # 单字学院（法学院→"法"）允许 1 字命中，其余至少 2 字。
    scored = sorted(((c if c >= min(2, len(_core(name)) or 2) else 0, name, url)
                     for name, url in colleges for c in [_common(want, _core(name))]), reverse=True)
    if not scored or scored[0][0] < 1:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0] and scored[1][2] != scored[0][2]:
        return None                       # 两个学院一样像，不猜
    return [scored[0][1], scored[0][2]]


def college_sites(school: str, domain: str, fetch, index_url: str | None = None) -> list[list[str]]:
    """从「机构设置 / 院系设置」页取出各学院的名称和网址（只收本校域名下的）。"""
    if not index_url:
        home = _homepage(domain, fetch)
        if not home:
            return []
        for url, text in _anchors(home[1], home[0]):
            if _in_domain(url, domain) and any(w == text or (w in text and len(text) <= 8)
                                               for w in COLLEGE_INDEX_WORDS):
                index_url = url
                break
    if not index_url:
        return []
    html = fetch(index_url)
    if not html:
        return []
    out: list[list[str]] = []
    for url, text in _anchors(html, index_url):
        if (_in_domain(url, domain) and 3 <= len(text) <= 22 and re.search(r"(学院|学部|系)$", text)
                and not any(w in text for w in ("继续教育", "网络教育", "培训", "孔子学院"))
                and [text, url] not in out):
            out.append([text, url])
    return out


def _college_columns(html: str, base: str, role: str = "source", domain: str = "") -> list[str]:
    """学院首页里的通知类栏目，只收这个学院自己目录（或子域）下的链接。

    学院表里的地址可能会跳转到学院自己的子域（华工 www.scut.edu.cn/sba/ → cnsba.scut.edu.cn），
    所以除了同目录，也接受本校域名下、但不是学校主站（www/www2）的独立子域。
    """
    prefix = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
    base_host = (urlparse(base).hostname or "").lower()
    shared = {base_host, f"www.{domain}", f"www2.{domain}", domain} if domain else {base_host}
    ranked: list[tuple[int, str]] = []
    for url, text, via_more in _labeled(html, base):
        if not text or len(text) > (16 if via_more else 10):
            continue
        if ARTICLE_RE.search(url):
            continue
        host = (urlparse(url).hostname or "").lower()
        own_dir = url.replace("http://", "https://").startswith(prefix.replace("http://", "https://"))
        own_host = bool(domain) and _in_domain(url, domain) and host not in shared
        if not (own_dir or own_host):
            continue
        for rank, word in enumerate(COLLEGE_COLUMN_WORDS[role]):
            if word in text:
                ranked.append((rank * 10 + (0 if text == word else 3) + (1 if via_more else 0), url))
                break
    out: list[str] = []
    for _, url in sorted(ranked):
        if url not in out:
            out.append(url)
    return out


def discover_college(school: str, college: str, url: str, fetch, *, max_fetches: int = 14,
                     role: str = "source", domain: str = "", say=lambda m: None) -> dict:
    used = 0

    def get(u: str) -> str | None:
        nonlocal used
        if used >= max_fetches:
            return None
        used += 1
        return fetch(u)

    entry = {"checked_at": _now().isoformat(timespec="seconds"), "archives": [], "college": college,
             "college_url": url, "rejects": []}
    html = get(url)
    if not html:
        entry["reason"] = "学院首页读不到"
        return entry
    for column in _college_columns(html, url, role, domain)[:4]:
        col_html = get(column)
        if not col_html:
            entry["rejects"].append({"url": column, "reason": "栏目页读不到"})
            continue
        archive, why = _validate_column(column, col_html, get)
        if not archive:
            entry["rejects"].append({"url": column, "reason": why})
            _dump(school, "college", column, col_html, why)
            continue
        title = next((t for u, t, _ in _labeled(html, url) if u == column), "通知")
        months, offset = DEFAULT_WINDOW[role]
        archive.update({"name": f"{school}{college}·{title}", "role": role, "months": months,
                        "year_offset": offset, "source": "auto-college"})
        entry["archives"].append(archive)
        say(f"    学院栏目：{school}{college} → {column}")
        if len(entry["archives"]) >= 2:       # 教务通知 + 通知公告 两个就够
            break
    if not entry["archives"]:
        entry["reason"] = "学院网站没找到可翻页的通知栏目"
    return entry


def college_archives_for(school: str, field_hint: str, fetch, *, domain: str | None = None,
                         deadline: float | None = None, allow_discovery: bool | None = None,
                         role: str = "source", say=lambda m: None,
                         path: Path = CACHE_PATH) -> list[dict]:
    """推荐方学院的通知栏目：人工配置的学院索引 > 首页里的「机构设置」。结果按学院缓存。"""
    from .plan import _school_record, school_domain, school_name
    if not school or not field_hint:
        return []
    name = school_name(school) or school
    record = _school_record(school) or {}
    domain = domain or school_domain(school)
    if allow_discovery is None:
        allow_discovery = os.environ.get("ARCHIVE_DISCOVERY", "on") != "off"
    listing = cached(name, "colleges", path)
    colleges = record.get("colleges") or (listing or {}).get("colleges")   # 人工核对过的学院表优先
    if colleges is None:
        if not allow_discovery or not domain:
            return []
        colleges = college_sites(name, domain, fetch, record.get("college_index"))
        save_entry(name, "colleges", {"checked_at": _now().isoformat(timespec="seconds"),
                                      "archives": [], "colleges": colleges}, path)
    picked = match_college(colleges, field_hint)
    if not picked:
        say(f"    学院栏目：{name} 按「{field_hint}」没对上唯一学院（共 {len(colleges)} 个学院）")
        return []
    college, url = picked
    key = f"college:{role}:{college}"
    hit = cached(name, key, path)
    if hit is not None:
        return hit.get("archives") or []
    if not allow_discovery or (deadline is not None and time.monotonic() >= deadline):
        return []
    entry = discover_college(name, college, url, fetch, role=role, domain=domain or "", say=say)
    save_entry(name, key, entry, path)
    return entry["archives"]

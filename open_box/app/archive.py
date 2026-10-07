"""学校通知栏目按日期定位：不经过搜索引擎，直接翻官网栏目找当年的公示。

为什么需要它（2026-10 一份真实简历实测）：哈工程本科生院的
《获得免试攻读2023年硕士学位研究生资格学生名单》公示，智谱搜索用正式标题、
公示年份、"免试 资格 名单"三种写法都没有返回——返回的全是各学院的《推免工作
实施细则》。那份公示是 2026-10-03 人工翻本科生院"工作通知"栏目第 19 页找到的。

栏目列表按日期倒序、每页固定条数，所以可以按日期**二分**定位到目标月份所在页，
实测哈工程本科生院栏目共 238 页，第 8 次读页定位到第 19 页的目标公示（2026-10-06）；
上限 12 页，不消耗搜索额度。只对 `data/schools.json` 里配置了
`notice_archives` 的学校启用；页码会随新公告变化，所以不硬编码页码，只写栏目地址。

配置示例（schools.json 某学校下）：
    "notice_archives": [{
        "name": "本科生院·工作通知",
        "first_page": "https://ugs.example.edu.cn/2821/list.htm",
        "list_url": "https://ugs.example.edu.cn/2821/list{page}.htm",
        "role": "source",                # source=推荐方（本科） / receiver=接收方
        "months": [8, 11],               # 目标公示月份区间
        "year_offset": -1                # 相对入学年份：推荐方公示在入学前一年秋季
    }]
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
HREF_DATE_RE = re.compile(r"/(20\d{2})/(\d{2})(\d{2})/|/(20\d{2})(\d{2})(\d{2})/|[/_t](20\d{2})(\d{2})(\d{2})_\d+\.")
PUSH_WORDS = ("免试", "推免", "推荐免试")
LIST_WORDS = ("名单", "资格", "公示")


@dataclass
class ArchiveItem:
    url: str
    title: str
    date: str          # YYYY-MM-DD


def _norm(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


# 很多栏目把日期拆成两块，只认完整日期会让这些列表一条都解析不出来（2026-10-07 批量
# 发现的失败页里统计到的写法）：
#   日 + 年月  <em>18</em><span>2026.09</span>          （清华研招）
#   年月 + 日  <span>2026-09</span><b>18</b>
#   月日 + 年  <i>03-02</i><b>2023</b> / <span>10-08</span>2021   （成信大、中财、徐医）
#   年 + 月日  <span>2026</span><span>04.22</span> / 2026 09/18  （天大研究生院、聊城）
#   两位年份   日期： 26/09/29 11:09:48（内蒙古师大）、26-09-04（广药、延边大学）
#   月日连年  06-112018（江西理工）；日 / 年月  01/ 2026-10（天津职师）
SPLIT_D_YM = re.compile(r"(?<![\d.\-/])(\d{1,2})\s*/?\s+(20\d{2})\s*[-/.年]\s*(\d{1,2})(?![\d.\-/])")
SPLIT_YM_D = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*月?\s+(\d{1,2})(?![\d.\-/])")
SPLIT_MD_Y = re.compile(r"(?<![\d.\-/])(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*(20\d{2})(?![\d.\-/])")
SPLIT_Y_MD = re.compile(r"(?<![\d.\-/])(20\d{2})\s*年?\s+(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?![\d.\-/])")
SHORT_YMD = re.compile(r"(?:日期|时间|发布)\s*[:：]?\s*(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?![\d.\-/])"
                       r"|(?<![\d.\-/:])([12]\d)-(0[1-9]|1[0-2])-([0-3]\d)(?![\d.\-/:])")


def find_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if m:
        y, mo, d = m.groups()
    elif (m := SPLIT_D_YM.search(text)):
        d, y, mo = m.groups()
    elif (m := SPLIT_YM_D.search(text)):
        y, mo, d = m.groups()
    elif (m := SPLIT_MD_Y.search(text)):
        mo, d, y = m.groups()
    elif (m := SPLIT_Y_MD.search(text)):
        y, mo, d = m.groups()
    elif (m := SHORT_YMD.search(text)):
        g = m.groups()
        y, mo, d = g[:3] if g[0] else g[3:]
        y = f"20{y}"
    else:
        return None
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    return _norm(y, mo, d)


# "更多 / 查看全文 / 详情" 之类的辅助链接：不算条目标题，也不该让条目容器被当成多条。
MORE_RE = re.compile(r"^[\[【（(]?\s*(更多|查看更多|了解更多|查看全部|查看全文|全文|详情|详细|阅读全文|阅读更多|"
                     r"more|MORE|More|READ\s*MORE)\s*[\]】）)]?\s*[>»+›]*$|^[>»+›]+$")


def _school_root(host: str) -> str:
    """xx.yy.edu.cn → yy.edu.cn；其他域名取后两段。"""
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("edu", "ac", "gov", "com", "org") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _same_site(url_host: str, base_host: str) -> bool:
    """栏目条目可以链到本校其他子站（新闻网），也可以直接链到学校公众号文章（天大教务处）。"""
    if url_host == base_host:
        return True
    if url_host in ("mp.weixin.qq.com",):
        return True
    return bool(url_host) and _school_root(url_host) == _school_root(base_host)


_TAG_RE = re.compile(r"^[\[【〔(（].{1,14}[\]】〕)）]$")


def _title_links(node, base_url: str = "") -> int:
    """节点里"像标题"的不同链接有几个。分类标签（[硕士招生]、【活动安排】）、
    "查看全文"、指回栏目本身的链接都不算；同一地址套了两层 <a> 也只算一个。"""
    hrefs: set[str] = set()
    for a in node.select("a[href]"):
        text = re.sub(r"\s+", "", a.get_text("", strip=True) or a.get("title", ""))
        if len(text) < 6 or MORE_RE.match(text) or _TAG_RE.match(text):
            continue
        url = urljoin(base_url, a.get("href", "")) if base_url else a.get("href", "")
        if base_url and _same_page(url, base_url):
            continue
        hrefs.add(url.split("#")[0])
    return len(hrefs)


def parse_list_page(html: str, base_url: str) -> tuple[list[ArchiveItem], int | None]:
    """取出栏目页里"标题 + 日期"的条目，以及总页数（取不到为 None）。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    host = urlparse(base_url).hostname or ""
    items: list[ArchiveItem] = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        url = urljoin(base_url, a.get("href", ""))
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        if not _same_site(urlparse(url).hostname or "", host):
            continue
        title = (a.get("title") or "").strip()
        text = a.get_text(" ", strip=True)
        if len(text) > len(title):
            title = text
        title = re.sub(r"\s+", " ", title)
        compact = title.replace(" ", "")
        if len(title) < 6 or MORE_RE.match(compact) or _TAG_RE.match(compact) or _same_page(url, base_url):
            continue
        date = find_date(text)              # 日期写在链接里面（天大教务处、徐医研招）
        node = a
        for _ in range(3 if not date else 0):   # 日期常在同一 <li>/<tr> 的兄弟节点里
            node = node.parent
            # 同一条目里除了标题还可能有分类标签、"查看全文"链接（中国科大、内蒙古师大、
            # 兰州财大）；只有出现第二个标题样的链接才说明再往上就是别的条目了。
            if node is None or _title_links(node, base_url) > 1:
                break
            date = find_date(node.get_text(" ", strip=True))
            if date:
                break
        if not date:
            # 标题和日期是相邻的两个块：<h3><a>标题</a></h3><div>发布时间：2026-04-16</div>（南方医科）
            for holder in (a, a.parent):
                sib = holder.find_next_sibling() if holder is not None else None
                if sib is not None and _title_links(sib, base_url) == 0:
                    date = find_date(sib.get_text(" ", strip=True))
                    if date:
                        break
        if not date:
            m = HREF_DATE_RE.search(url)
            if m:
                date = _norm(*[g for g in m.groups() if g])
        if not date:
            continue
        seen.add(url)
        items.append(ArchiveItem(url=url, title=title, date=date))

    total = declared_pages(html)
    if total is None:
        pages = [int(n) for n in re.findall(r'list(\d+)\.(?:htm|html|psp)', html or "")]
        total = max(pages) if pages else None
    return items, total


NEXT_WORDS = ("下一页", "下页", "后一页", "后页")
LAST_WORDS = ("尾页", "末页", "最后一页", "最末页", "最后页")
_NUM = re.compile(r"\d+")

# 页面上写出来的总页数："共 238 页"、"共89条，分5页"、"共71条，8页，当前第 1 页"、"共55条 1/7"
_DECLARED = (r'all_pages[^>]*>\s*(\d+)', r'共\s*(\d+)\s*页', r'分\s*(\d+)\s*页',
             r'(\d+)\s*页\s*[，,]?\s*当前', r'条\s*(?:&nbsp;|\s)*1\s*/\s*(\d+)(?!\d)',
             r'(?<![\d/])1\s*/\s*(\d+)\s*(?:&nbsp;|\s)*(?:首页|上一?页|<)', r'/\s*(\d+)\s*页')


def declared_pages(html: str) -> int | None:
    html = re.sub(r"&nbsp;|\xa0|&#160;", " ", html or "")
    for pattern in _DECLARED:
        m = re.search(pattern, html)
        if m and 0 < int(m.group(1)) < 5000:
            return int(m.group(1))
    return None


def _same_page(a: str, b: str) -> bool:
    def norm(u: str) -> str:
        u = re.sub(r"^https?://", "", u).split("#")[0].rstrip("/")
        return re.sub(r"/(index|default|main)\.(s?html?|jsp|php|aspx?)$", "", u)
    return norm(a) == norm(b)


def _link(soup, words: tuple[str, ...], css: str, base_url: str = "") -> str | None:
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript", "#")):
            continue
        if base_url and _same_page(urljoin(base_url, href), base_url):
            continue                        # 只有一页时"下一页"常常指回本页
        text = a.get_text(" ", strip=True)
        classes = " ".join(a.get("class") or [])
        if any(w in text for w in words) or css in classes.split():
            return href
    return None


def _numbered(soup, n: int, base_url: str) -> str | None:
    """页码条里写着 n 的链接（没有"下一页"字样时用它当第 2 页）。"""
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if a.get_text("", strip=True) == str(n) and href and not href.startswith(("javascript", "#")) \
                and not _same_page(urljoin(base_url, href), base_url):
            return href
    return None


def pagination(html: str, base_url: str) -> dict | None:
    """从栏目页的"下一页 / 尾页"链接推出翻页规律。

    常见建站系统：
      · WebPlus（list.htm → list2.htm … list238.htm）：页码**递增**，越往后越旧；
      · 博达 VSB（tzgg.htm → tzgg/56.htm … tzgg/1.htm）：页码**递减**，第 2 页的编号
        最大，尾页是 1。新公告一多，编号整体会变，所以运行时每次从第 1 页重新推；
      · TRS（index.htm → index1.htm … index82.htm）：第 N 页的编号是 N-1；
      · WebPlus 动态页（list.jsp?…&a123p=2&…）：页码在查询参数里。
    返回 {"template", "order": forward|reverse, "total", "offset"}；推不出来返回 None。
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    nxt = _link(soup, NEXT_WORDS, "next", base_url) or _numbered(soup, 2, base_url)
    last = _link(soup, LAST_WORDS, "last", base_url)
    if not nxt:
        return None
    nxt_url = urljoin(base_url, nxt)
    last_url = urljoin(base_url, last) if last else None
    n_nums = list(_NUM.finditer(nxt_url))
    if not n_nums:
        return None
    declared = declared_pages(html)
    if last_url == nxt_url:
        # 只有两页：下一页就是尾页（VSB 的 tzgg/1.htm、TRS 的 index1.htm 都是这样）。
        last_url, declared = None, declared or 2

    def differing(other: str | None):
        """和"下一页"只差一段数字的另一个翻页链接 → 那段数字就是页码。"""
        if not other:
            return None
        o_nums = list(_NUM.finditer(other))
        if len(o_nums) != len(n_nums):
            return None
        diffs = [(a, b) for a, b in zip(n_nums, o_nums) if a.group() != b.group()]
        if len(diffs) == 1 and nxt_url[:diffs[0][0].start()] == other[:diffs[0][1].start()]:
            return diffs[0]
        return None

    pick, last_num = n_nums[-1], None
    d = differing(last_url)
    if d:
        pick, last_num = d[0], int(d[1].group())
    else:
        last_url = None
        third = _numbered(soup, 3, base_url)
        d3 = differing(urljoin(base_url, third)) if third else None
        if d3:
            pick = d3[0]
        else:
            # 没有尾页、也没有第 3 页可比：取等于 1 或 2 的那段数字（查询参数里常有别的编号）。
            small = [m for m in n_nums if m.group() in ("1", "2")]
            if small:
                pick = small[-1]
    template = nxt_url[:pick.start()] + "{page}" + nxt_url[pick.end():]
    next_num = int(pick.group())
    if next_num == 2:
        total = last_num if last_url else declared
        return {"template": template, "order": "forward", "total": total, "offset": 0}
    if next_num == 1:
        # TRS 等系统：index.htm → index1.htm … index82.htm，第 N 页的编号是 N-1。
        # 只有两页的 VSB 栏目（下一页 = 尾页 = tzgg/1.htm）按这个规律算出来的地址也一样。
        total = (last_num + 1) if last_url and last_num and last_num > 1 else declared
        return {"template": template, "order": "forward", "total": total, "offset": -1}
    if last_url and last_num == 1 and next_num > 2:
        return {"template": template, "order": "reverse", "total": next_num + 1}
    if not last_url and next_num > 2 and declared and next_num == declared - 1:
        return {"template": template, "order": "reverse", "total": declared}
    return None


def page_url(archive: dict, page: int, total: int | None = None) -> str:
    if page <= 1:
        return archive["first_page"]
    if archive.get("page_order") == "reverse":
        if not total:
            raise ValueError("倒序翻页需要总页数")
        return archive["list_url"].format(page=total - page + 1)
    return archive["list_url"].format(page=page + int(archive.get("page_offset", 0)))


def target_window(archive: dict, intake_year: int) -> tuple[str, str]:
    year = intake_year + int(archive.get("year_offset", 0))
    start_m, end_m = (archive.get("months") or [1, 12])[:2]
    return f"{year:04d}-{int(start_m):02d}-01", f"{year:04d}-{int(end_m):02d}-31"


def locate_notices(archive: dict, intake_year: int, fetch, *, max_pages: int = 12,
                   deadline: float | None = None, say=lambda m: None,
                   words_any: tuple[str, ...] = PUSH_WORDS,
                   words_list: tuple[str, ...] = LIST_WORDS) -> list[ArchiveItem]:
    """在栏目里二分到目标月份，返回日期落在窗口内、标题像推免名单的公告。

    `fetch(url) -> str | None` 用调用方的 PageFetcher（同样守 robots、限流）。
    读不到就停，不猜页码；返回空列表不代表学校没有公示。
    """
    start, end = target_window(archive, intake_year)
    cache: dict[int, list[ArchiveItem]] = {}
    total: int | None = None
    used = 0

    reverse = archive.get("page_order") == "reverse"
    if archive.get("page_order") == "single":
        total = 1                           # 只有一页的栏目（条目少、没有翻页）

    def load(page: int) -> list[ArchiveItem] | None:
        nonlocal total, used
        if page in cache:
            return cache[page]
        if used >= max_pages or (deadline is not None and time.monotonic() >= deadline):
            return None
        if page > 1 and reverse and not total:
            return None
        url = page_url(archive, page, total)
        used += 1
        html = fetch(url)
        if not html:
            return None
        items, pages = parse_list_page(html, url)
        if page == 1:
            pag = pagination(html, url)
            if pag and pag.get("total"):
                pages = pag["total"] if reverse or not pages else max(pages, pag["total"])
        if pages and (total is None or (not reverse and pages > total) or page == 1):
            total = pages
        cache[page] = items
        return items

    first = load(1)
    if not first:
        say(f"    栏目定位：{archive.get('name', '通知栏目')} 第 1 页读不到，跳过")
        return []
    lo, hi = 1, total or 1
    if total is None:
        # 没有总页数时倍增探测，直到翻到比目标窗口更早的页。
        probe = 2
        while probe <= 128:
            items = load(probe)
            if not items:
                break
            hi = probe
            if min(i.date for i in items) < start:
                break
            probe *= 2
    found: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        items = load(mid)
        if not items:
            break
        newest, oldest = max(i.date for i in items), min(i.date for i in items)
        if oldest > end:
            lo = mid + 1
        elif newest < start:
            hi = mid - 1
        else:
            found = mid
            break
    if found is None:
        say(f"    栏目定位：{archive.get('name', '通知栏目')} 未定位到 {start[:7]}–{end[:7]} "
            f"的页（读了 {used} 页）")
        return []
    pages = [found]
    for neighbour in (found - 1, found + 1):   # 窗口可能跨页
        if neighbour >= 1 and (total is None or neighbour <= total) and load(neighbour):
            pages.append(neighbour)
    hits = []
    for p in pages:
        for item in cache.get(p, []):
            if start <= item.date <= end and any(w in item.title for w in words_any) \
                    and any(w in item.title for w in words_list):
                hits.append(item)
    say(f"    栏目定位：{archive.get('name', '通知栏目')} 第 {sorted(pages)} 页"
        f"（共 {total or '?'} 页，读了 {used} 页），找到 {len(hits)} 条候选公示")
    return hits

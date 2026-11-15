"""学校通知栏目自动发现与翻页规律（app/discover.py、app/archive.pagination）。站点均为虚构。"""
import json
import tempfile
import unittest
from pathlib import Path

from app import discover
from app.archive import locate_notices, pagination


def _rows(items):
    return "".join(f'<tr><td><a href="{u}" title="{t}">{t}</a></td><td class="date">{d}</td></tr>'
                   for u, t, d in items)


def _items(page, per=10, start_year=2026):
    """第 page 页（1 起）的 10 条通知，每页往前推约一个月。"""
    out = []
    for i in range(per):
        k = (page - 1) * per + i
        y, m = start_year - k // 120, 12 - (k // 10) % 12
        out.append((f"/{y}/{m:02d}{(28 - i):02d}/c1a{k}/page.htm", f"关于第{k}项教学工作的通知",
                    f"{y}-{m:02d}-{28 - i:02d}"))
    return out


def webplus_site(domain="x.edu.cn", pages=60, target=None):
    base = f"https://jwc.{domain}"
    site = {
        f"https://www.{domain}/": f'<title>某某大学</title><a href="https://jwc.{domain}/">教务处</a>',
        f"{base}/": '<title>某某大学教务处</title><a href="/2821/list.htm">工作通知</a><a href="/about.htm">处室简介</a>',
    }
    for p in range(1, pages + 1):
        items = _items(p)
        if target and p == target[0]:
            items[3] = target[1]
        nav = (f'<a class="next" href="/2821/list{p + 1}.htm">下一页</a>'
               f'<a class="last" href="/2821/list{pages}.htm">尾页</a>') if p < pages else ""
        url = f"{base}/2821/list.htm" if p == 1 else f"{base}/2821/list{p}.htm"
        site[url] = f"<table>{_rows(items)}</table>{nav}"
    return site


def vsb_site(domain="y.edu.cn", pages=40):
    base = f"https://yz.{domain}"
    site = {f"https://www.{domain}/": '<title>某某大学</title><a href="https://yz.y.edu.cn/">研究生招生</a>',
            f"{base}/": '<title>某某大学研究生招生网</title><a href="tzgg.htm">通知公告</a>'}
    for p in range(1, pages + 1):
        num = pages - p + 1
        nxt = f'<a href="tzgg/{num - 1}.htm">下页</a><a href="tzgg/1.htm">尾页</a>' if p < pages else ""
        url = f"{base}/tzgg.htm" if p == 1 else f"{base}/tzgg/{num}.htm"
        prefix = "" if p == 1 else "../"
        site[url] = f"<table>{_rows(_items(p))}</table>" + nxt.replace('href="', f'href="{prefix}')
    return site


class PaginationTests(unittest.TestCase):
    def test_webplus_forward(self):
        site = webplus_site()
        pag = pagination(site["https://jwc.x.edu.cn/2821/list.htm"], "https://jwc.x.edu.cn/2821/list.htm")
        self.assertEqual(pag, {"template": "https://jwc.x.edu.cn/2821/list{page}.htm",
                               "order": "forward", "total": 60, "offset": 0})

    def test_vsb_reverse(self):
        site = vsb_site()
        pag = pagination(site["https://yz.y.edu.cn/tzgg.htm"], "https://yz.y.edu.cn/tzgg.htm")
        self.assertEqual(pag, {"template": "https://yz.y.edu.cn/tzgg/{page}.htm",
                               "order": "reverse", "total": 40})

    def test_page_without_paging_links_is_rejected(self):
        self.assertIsNone(pagination("<a href='/a.htm'>首页</a>", "https://z.edu.cn/"))


class RealWorldFormatTests(unittest.TestCase):
    """2026-10-07 用内置浏览器核对过的真实栏目格式（内容为仿写）。"""

    def test_split_day_and_year_month_dates(self):
        from app.archive import parse_list_page
        html = "".join(f'<li><a href="info/1024/{i}.htm"><div class="time"><em>{18 - i}</em>'
                       f'<span>2026.09</span></div><div class="name">第{i}条推免通知</div></a></li>'
                       for i in range(6))
        items, _ = parse_list_page(f"<ul>{html}</ul>", "https://yz.z.edu.cn/zxgg.htm")
        self.assertEqual([i.date for i in items][:2], ["2026-09-18", "2026-09-17"])

    def test_trs_index_paging_is_offset_by_one(self):
        html = ('<a class="gp-page-next" href="index1.htm"><span>下一页</span></a>'
                '<a class="gp-page-end" href="index82.htm"><span>末页</span></a>')
        pag = pagination(html, "https://jwc.z.edu.cn/jwtz/index.htm")
        self.assertEqual(pag, {"template": "https://jwc.z.edu.cn/jwtz/index{page}.htm",
                               "order": "forward", "total": 83, "offset": -1})
        from app.archive import page_url
        archive = {"first_page": "https://jwc.z.edu.cn/jwtz/index.htm", "list_url": pag["template"],
                   "page_order": "forward", "page_offset": -1}
        self.assertEqual(page_url(archive, 2), "https://jwc.z.edu.cn/jwtz/index1.htm")

    def test_single_page_column_is_accepted(self):
        rows = _rows(_items(1))
        html = f'<table>{rows}</table><span class="p_next_d">下页</span><span class="p_last_d">尾页</span>'
        archive, why = discover._validate_column("https://yz.z.edu.cn/zxgg.htm", html, lambda u: None)
        self.assertEqual(archive["page_order"], "single", why)

    def test_guessed_subdomain_that_is_not_the_academic_office_is_skipped(self):
        site = {"https://www.z.edu.cn/": "<title>某某大学</title>",
                "https://jwb.z.edu.cn/": '<title>某某大学纪检监察网</title><a href="/8308/list.htm">通知公告</a>'}
        entry = discover.discover("某某大学", "z.edu.cn", "source", site.get)
        self.assertNotIn("https://jwb.z.edu.cn/8308/list.htm", entry["tried"])
        self.assertTrue(any("站点名称不像" in r["reason"] for r in entry["rejects"]))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "cache.json"

    def test_finds_office_from_homepage_and_validates_column(self):
        site = webplus_site()
        entry = discover.discover("某某大学", "x.edu.cn", "source", site.get)
        self.assertEqual(len(entry["archives"]), 1)
        archive = entry["archives"][0]
        self.assertEqual(archive["first_page"], "https://jwc.x.edu.cn/2821/list.htm")
        self.assertEqual(archive["page_order"], "forward")
        self.assertEqual((archive["months"], archive["year_offset"]), ([8, 11], -1))

    def test_reverse_numbered_vsb_column_is_found_and_searchable(self):
        site = vsb_site()
        entry = discover.discover("某某大学", "y.edu.cn", "receiver", site.get)
        archive = entry["archives"][0]
        self.assertEqual(archive["page_order"], "reverse")
        # 2024 年 10 月（第 27 页，倒序编号 14）能按倒序编号读到
        hits = locate_notices({**archive, "months": [10, 10], "year_offset": 0}, 2024, site.get,
                              words_any=("教学",), words_list=("通知",))
        self.assertTrue(hits and all(h.date.startswith("2024-10") for h in hits))

    def test_failure_is_cached_and_not_retried(self):
        calls = []

        def nothing(url):
            calls.append(url)
            return None

        first = discover.archives_for("某某大学", "source", nothing, domain="none.edu.cn",
                                      allow_discovery=True, path=self.cache)
        n = len(calls)
        second = discover.archives_for("某某大学", "source", nothing, domain="none.edu.cn",
                                       allow_discovery=True, path=self.cache)
        self.assertEqual((first, second), ([], []))
        self.assertEqual(len(calls), n)              # 第二次直接读缓存
        data = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertIn("某某大学", data["schools"])

    def test_manual_config_wins_over_discovery(self):
        archives = discover.archives_for("哈尔滨工程大学", "source", lambda url: None,
                                         allow_discovery=True, path=self.cache)
        self.assertEqual(archives[0]["first_page"], "https://ugs.hrbeu.edu.cn/2821/list.htm")
        self.assertFalse(self.cache.exists())

    def test_domain_check_requires_school_name_on_homepage(self):
        site = {"https://www.x.edu.cn/": "<title>另一所学院</title>"}
        self.assertFalse(discover.check_domain("某某大学", "x.edu.cn", site.get)["ok"])
        site = {"https://www.x.edu.cn/": "<title>某某大学</title>"}
        self.assertTrue(discover.check_domain("某某大学", "x.edu.cn", site.get)["ok"])


class CollegeTests(unittest.TestCase):
    """推免名单只发在学院网站的学校（仿华南理工：机构设置页 → 学院 → 教务通知）。"""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "cache.json"

    def _site(self):
        base = "https://www2.c.edu.cn/cs"
        site = {
            "https://www.c.edu.cn/": '<title>某某理工大学</title><a href="/new/8996/list.htm">机构设置</a>',
            "https://www.c.edu.cn/new/8996/list.htm":
                '<a href="https://www2.c.edu.cn/cs/">计算机科学与工程学院</a>'
                '<a href="https://www2.c.edu.cn/ee/">电子与信息学院</a>'
                '<a href="https://sce.c.edu.cn/">继续教育学院</a>',
            f"{base}/": '<title>计算机学院</title><a href="/cs/jwtz/list.htm">教务通知</a>'
                        '<a href="/cs/xw/list.htm">学院新闻</a>',
        }
        for p in range(1, 31):
            items = _items(p)
            if p == 16:                    # 2025 年 9 月前后那一页
                items[2] = ("/cs/2025/0821/c45217a1/page.htm",
                            "计算机科学与工程学院关于公示拟推荐免试攻读研究生名单的通知", "2025-08-21")
            url = f"{base}/jwtz/list.htm" if p == 1 else f"{base}/jwtz/list{p}.htm"
            nav = (f'<a class="next" href="/cs/jwtz/list{p + 1}.htm">下一页</a>'
                   f'<a class="last" href="/cs/jwtz/list30.htm">尾页</a>') if p < 30 else ""
            site[url] = f"<table>{_rows(items)}</table>{nav}"
        return site

    def test_match_college_by_major(self):
        colleges = [["计算机科学与工程学院", "u1"], ["电子与信息学院", "u2"], ["软件学院", "u3"]]
        self.assertEqual(discover.match_college(colleges, "计算机科学与技术"), ["计算机科学与工程学院", "u1"])
        self.assertIsNone(discover.match_college(colleges, "法学"))

    def test_college_list_skips_continuing_education(self):
        site = self._site()
        colleges = discover.college_sites("某某理工大学", "c.edu.cn", site.get)
        self.assertEqual([c[0] for c in colleges], ["计算机科学与工程学院", "电子与信息学院"])

    def test_push_roster_on_college_site_is_located(self):
        site = self._site()
        archives = discover.college_archives_for("某某理工大学", "计算机科学与技术", site.get,
                                                 domain="c.edu.cn", allow_discovery=True, path=self.cache)
        self.assertEqual(archives[0]["first_page"], "https://www2.c.edu.cn/cs/jwtz/list.htm")
        hits = locate_notices(archives[0], 2026, site.get)
        self.assertEqual([h.date for h in hits], ["2025-08-21"])
        # 第二次直接用缓存（学院列表、学院栏目都缓存了）
        calls = []
        again = discover.college_archives_for("某某理工大学", "计算机科学与技术",
                                              lambda u: calls.append(u), domain="c.edu.cn",
                                              allow_discovery=True, path=self.cache)
        self.assertEqual((again[0]["first_page"], calls), (archives[0]["first_page"], []))


class ReceiverCollegeTests(unittest.TestCase):
    """仿华南理工工商管理学院：学院表地址跳到独立子域，「招生资讯」里有推免复试拟录取名单（HTML 表格）。"""

    def _site(self):
        base = "https://cnsba.c.edu.cn"
        site = {
            "https://www.c.edu.cn/sba/": '<title>工商管理学院</title>'
                                         f'<a href="{base}/zszx/list.htm">招生资讯</a>'
                                         f'<a href="{base}/bkstz/list.htm">本科生通知</a>'
                                         '<a href="https://www.c.edu.cn/new/">华工主页</a>',
        }
        notice = f"{base}/2025/1015/c24701a605483/page.htm"
        for p in range(1, 6):
            items = _items(p, start_year=2025)          # 第 2 页约为 2025 年 11 月
            if p == 2:
                items[1] = ("/2025/1015/c24701a605483/page.htm",
                            "关于公示2026年学术型研究生推免复试拟录取结果的通知", "2025-10-15")
            url = f"{base}/zszx/list.htm" if p == 1 else f"{base}/zszx/list{p}.htm"
            nav = (f'<a class="next" href="/zszx/list{p + 1}.htm">下一页</a>'
                   f'<a class="last" href="/zszx/list5.htm">尾页</a>') if p < 5 else ""
            site[url] = f"<table>{_rows(items)}</table>{nav}"
        site[notice] = ('<div class="wp_articlecontent"><p>现将拟录取结果予以公示</p><table>'
                        '<tr><td>报名号</td><td>姓名</td><td>录取专业</td></tr>'
                        '<tr><td>202607450</td><td>张三</td><td>管理科学与工程</td></tr></table></div>')
        return site, notice

    def test_columns_on_redirected_college_host_are_accepted(self):
        site, _ = self._site()
        cols = discover._college_columns(site["https://www.c.edu.cn/sba/"], "https://www.c.edu.cn/sba/",
                                         "receiver", "c.edu.cn")
        self.assertEqual(cols[0], "https://cnsba.c.edu.cn/zszx/list.htm")
        self.assertNotIn("https://www.c.edu.cn/new/", cols)

    def test_receiver_claim_reaches_college_admission_roster(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.collect import Budget, collect_for_claim
        from app.schema import Claim
        site, notice = self._site()
        claim = Claim(id="g", raw_text="2025.09 某某理工大学（保研） 管理科学与工程", raw_locator="教育",
                      category="学历", date_label="2026.09", date_start="2026-09",
                      elements=["学校=某某理工大学", "入学方式=保研"],
                      entities={"org": "某某理工大学", "dept": "工商管理学院"})
        seen = []
        cache = Path(tempfile.mkdtemp()) / "c.json"
        record = {"name": "某某理工大学", "colleges": [["工商管理学院", "https://www.c.edu.cn/sba/"]]}
        with patch("app.plan._school_record", side_effect=lambda n: record if n and "某某理工" in n else None), \
             patch("app.discover.CACHE_PATH", cache), \
             patch.dict("os.environ", {"ARCHIVE_DISCOVERY": "on", "ARCHIVE_CACHE": str(cache)}), \
             patch("app.collect.school_domain", side_effect=lambda n: "c.edu.cn" if n and "某某理工" in n else None), \
             patch("app.collect.extract_evidence",
                   side_effect=lambda c, hit, text, llm, **kw: seen.append((hit.url, text))):
            collect_for_claim(claim, "张三", SimpleNamespace(search=lambda q, site=None: []), llm=None,
                              queries=[], budget=Budget(max_searches=0, max_page_reads=4),
                              fetcher=SimpleNamespace(get=site.get, failures={}))
        self.assertTrue(any(u == notice and "张三" in t for u, t in seen), seen)


class SecondRunFormatTests(unittest.TestCase):
    """2026-10-07 第二轮批量发现的失败页里统计出来的写法（内容为仿写）。"""

    def test_more_link_takes_heading_of_its_block(self):
        # 中财教务处 / 哈工大本科生院：栏目标题不是链接，只有"更多>>"能点
        html = ('<title>某某大学教务处</title>'
                '<div class="box"><div class="hd"><span>新闻动态</span><a href="xwdt.htm">更多>></a></div></div>'
                '<div class="box"><div class="hd"><h3>通知公告</h3><a href="tzgg.htm">更多>></a></div>'
                '<ul><li><a href="info/1/1.htm">关于某项工作的通知</a></li></ul></div>')
        cols = discover._columns(html, "https://jwc.m.edu.cn/", "m.edu.cn", "source")
        self.assertEqual(cols[0], "https://jwc.m.edu.cn/tzgg.htm")
        self.assertNotIn("https://jwc.m.edu.cn/info/1/1.htm", cols)

    def test_push_column_ranks_first_and_slug_is_a_fallback(self):
        html = ('<a href="/index/tzgg.htm">通知公告</a><a href="/index/xsfw1/tmyjs.htm">推免研究生</a>'
                '<a href="/zxtz/list.htm"><img src="t.png"></a>')
        cols = discover._columns(html, "https://bksy.m.edu.cn/", "m.edu.cn", "source")
        self.assertEqual(cols[:2], ["https://bksy.m.edu.cn/index/xsfw1/tmyjs.htm",
                                    "https://bksy.m.edu.cn/index/tzgg.htm"])

    def test_more_dates_formats(self):
        from app.archive import find_date
        cases = {"03-02 2023": "2023-03-02", "2026 04.22": "2026-04-22",
                 "2026 09/18 关于": "2026-09-18", "26-09-04": "2026-09-04",
                 "日期： 26/09/29 11:09:48": "2026-09-29", "06-112018关于": "2018-06-11",
                 "01/ 2026-10": "2026-10-01", "电话 0471-4393150": None, "2025-2026学年": None}
        for text, want in cases.items():
            self.assertEqual(find_date(text), want, text)

    def test_item_with_category_tag_and_sibling_date(self):
        from app.archive import parse_list_page
        # 兰州财大：同一行里有 [硕士招生] 分类链接；南方医科：日期在标题后面的兄弟块里
        rows = "".join(f'<tr><td><a href="sszs.htm">[硕士招生]</a><a href="../info/1/{i}.htm">第{i}条招生通知标题</a></td>'
                       f'<td>2026-09-{20 - i:02d}</td></tr>' for i in range(6))
        items, _ = parse_list_page(f"<table>{rows}</table>", "https://yjsy.m.edu.cn/zsgz/sszs.htm")
        self.assertEqual(len(items), 6)
        self.assertNotIn("[硕士招生]", [i.title for i in items])
        blocks = "".join(f'<h3><a href="info/1/{i}.htm">第{i}条硕士招生公告</a></h3>'
                         f'<div class="newsinfo">发布时间：2026-04-{20 - i:02d} 16:00</div>' for i in range(6))
        items, _ = parse_list_page(f'<div class="list">{blocks}</div>', "https://portal.m.edu.cn/yzw/sszs.htm")
        self.assertEqual([i.date for i in items][:2], ["2026-04-20", "2026-04-19"])

    def test_items_linking_to_wechat_articles_are_kept(self):
        from app.archive import parse_list_page
        rows = "".join(f'<li><a href="https://mp.weixin.qq.com/s/a{i}"><h2>第{i}条教务通知标题</h2>'
                       f'<span>2026-05-{20 - i:02d}</span></a></li>' for i in range(6))
        items, _ = parse_list_page(f"<ul>{rows}</ul>", "http://oaa.m.edu.cn/bszy/tzgg.htm")
        self.assertEqual(len(items), 6)

    def test_two_page_vsb_and_trs_without_last_link(self):
        # 下一页 = 尾页 = tzgg/1.htm：只有两页
        html = '<a href="tzgg/1.htm">2</a><a href="tzgg/1.htm">下页</a><a href="tzgg/1.htm">尾页</a>'
        pag = pagination(html, "http://grad.m.edu.cn/xwgl/tzgg.htm")
        self.assertEqual((pag["total"], pag["offset"]), (2, -1))
        from app.archive import page_url
        archive = {"first_page": "x", "list_url": pag["template"], "page_order": pag["order"],
                   "page_offset": pag["offset"]}
        self.assertEqual(page_url(archive, 2), "http://grad.m.edu.cn/xwgl/tzgg/1.htm")
        # TRS 没有尾页链接，总页数写在文字里
        html = '共1782条新闻，分149页，当前第 1 页 <a href="index.htm">上一页</a><a href="index1.htm">下一页</a>'
        pag = pagination(html, "https://grs.m.edu.cn/tzgg/index.htm")
        self.assertEqual((pag["total"], pag["offset"]), (149, -1))
        html = '共73条，分&nbsp;7&nbsp;页 <a href="index1.html">下一页</a>'
        self.assertEqual(pagination(html, "https://g.m.edu.cn/sszs/index.html")["total"], 7)

    def test_query_parameter_paging_picks_the_page_number(self):
        q = "?a246098t=6&a246098p={}&a246098c=20&urltype=tree.TreeTempUrl&wbtreeid=1081"
        html = "".join(f'<a href="{q.format(n)}">{n}</a>' for n in (2, 3, 4)) + f'<a href="{q.format(2)}">下页</a>'
        pag = pagination(html, "http://graduate.m.edu.cn/zsnew-list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1081")
        self.assertIn("a246098p={page}", pag["template"])
        self.assertEqual(pag["order"], "forward")

    def test_next_link_pointing_back_to_same_page_means_single_page(self):
        rows = _rows(_items(1))
        html = f'<table>{rows}</table><a href="index.htm">上一页</a><a href="index.htm">下一页</a>'
        archive, why = discover._validate_column("https://yjs.m.edu.cn/xwgl/tzgg/index.htm", html, lambda u: None)
        self.assertEqual(archive["page_order"], "single", why)

    def test_short_push_column_is_accepted(self):
        rows = "".join(f'<tr><td><a href="/info/{y}.htm">某某大学{y}年接收优秀应届本科毕业生免试攻读研究生的通知</a></td>'
                       f'<td>{y - 1}-09-16</td></tr>' for y in (2027, 2026, 2025, 2024))
        archive, why = discover._validate_column("https://yjszs.m.edu.cn/tzgg1/tms.htm", f"<table>{rows}</table>",
                                                 lambda u: None)
        self.assertEqual(archive and archive["page_order"], "single", why)

    def test_intro_page_on_main_site_leads_to_office_subsite(self):
        site = webplus_site(domain="h.edu.cn")
        site["https://www.h.edu.cn/"] = ('<title>某某大学</title>'
                                         '<a href="https://www.h.edu.cn/jyjx/bksjy.htm">本科生教育</a>')
        site["https://www.h.edu.cn/jyjx/bksjy.htm"] = ('<title>本科生教育-某某大学</title>'
                                                      '<a href="https://jwc.h.edu.cn/">本科生院主页</a>')
        site.pop("https://jwc.h.edu.cn/")
        site["https://jwc.h.edu.cn/"] = '<title>某某大学本科生院</title><a href="/2821/list.htm">工作通知</a>'
        # 子域猜测里没有 jwc. 也能从介绍页跳过去
        from unittest.mock import patch
        with patch.dict(discover.PREFIXES, {"source": ["ugs"]}):
            entry = discover.discover("某某大学", "h.edu.cn", "source", site.get)
        self.assertEqual(entry["archives"][0]["first_page"], "https://jwc.h.edu.cn/2821/list.htm")

    def test_literal_redirect_stub_and_login_system_pages(self):
        site = webplus_site(domain="n.edu.cn")
        site["https://www.n.edu.cn/"] = ('<title>某某大学</title><a href="https://jw.n.edu.cn/">教务处</a>'
                                         '<a href="https://bksy.n.edu.cn/">本科生院</a>')
        site["https://bksy.n.edu.cn/"] = '<html><script>location.href="//jwc.n.edu.cn/";</script></html>'
        site["https://jw.n.edu.cn/"] = '<title>某某大学综合教务管理系统-登录</title>'
        from unittest.mock import patch
        with patch.dict(discover.PREFIXES, {"source": []}):
            entry = discover.discover("某某大学", "n.edu.cn", "source", site.get)
        self.assertEqual(entry["archives"][0]["first_page"], "https://jwc.n.edu.cn/2821/list.htm")
        self.assertTrue(any("登录/管理系统" in r["reason"] for r in entry["rejects"]))

    def test_domain_check_accepts_alias_and_names_waf(self):
        from unittest.mock import patch
        with patch("app.plan._school_record", return_value={"aliases": ["某某医科大学"]}):
            site = {"https://www.w.edu.cn/": "<title>某某医科大学</title>"}
            self.assertTrue(discover.check_domain("某某医学院", "w.edu.cn", site.get)["ok"])
        site = {"https://www.w.edu.cn/": "<title>WEB应用防火墙</title>"}
        self.assertIn("防火墙", discover.check_domain("某某大学", "w.edu.cn", site.get)["reason"])

    def test_gbk_page_without_charset_header_is_decoded(self):
        from types import SimpleNamespace
        from app.search import decode_html
        body = "<title>大连某某大学</title>".encode("gbk")
        self.assertIn("大连某某大学", decode_html(SimpleNamespace(content=body, headers={"content-type": "text/html"})))
        body = '<meta charset="gb2312"><title>大连某某大学</title>'.encode("gbk")
        self.assertIn("大连某某大学", decode_html(SimpleNamespace(content=body, headers={})))



class ThirdRunTests(unittest.TestCase):
    """2026-10-07 第三轮批量（新规则首跑）后剩下的失败页（内容为仿写）。"""

    def test_heading_with_english_subtitle_and_fullwidth_more(self):
        html = ('<div class="t"><h3>通知公告<span>Announcements</span></h3><a href="/tzgg/list.htm">Ｍore+</a></div>'
                '<div class="t"><h3>办事指南<span>Guide</span></h3><a href="/9963/list.htm">Ｍore+</a></div>')
        self.assertEqual(discover._columns(html, "http://jw.m.edu.cn/", "m.edu.cn", "source"),
                         ["http://jw.m.edu.cn/tzgg/list.htm"])

    def test_tabbed_block_maps_more_links_in_order(self):
        html = ('<div class="tt1"><h3><span>教务公告</span><span>招生专栏</span></h3><div class="more_btn1">'
                '<a href="/5113/list.htm"><span>更多+</span></a><a href="/5200/list.htm"><span>更多+</span></a></div></div>')
        labels = {u: t for u, t, _ in discover._labeled(html, "http://jwc.m.edu.cn/")}
        self.assertEqual(labels["http://jwc.m.edu.cn/5113/list.htm"], "教务公告")
        self.assertEqual(labels["http://jwc.m.edu.cn/5200/list.htm"], "招生专栏")

    def test_unlabeled_more_link_still_ranked_by_slug_and_jsp_column_kept(self):
        html = '<div><a href="lbxw/index.htm">更多>></a></div><div><a href="tztg/index.htm">更多>></a></div>'
        self.assertEqual(discover._columns(html, "https://jwc.m.edu.cn/", "m.edu.cn", "source"),
                         ["https://jwc.m.edu.cn/tztg/index.htm"])
        html = '<div><span>通知公告</span><a href="info/iList.jsp?cat_id=10495">更多</a></div>'
        self.assertEqual(discover._columns(html, "https://jwc.m.edu.cn/", "m.edu.cn", "source"),
                         ["https://jwc.m.edu.cn/info/iList.jsp?cat_id=10495"])

    def test_http_fallback_when_guessed_https_subdomain_has_wrong_certificate(self):
        # 子站整个只开了 http
        site = {k.replace("https://jwc.", "http://jwc."): v for k, v in webplus_site(domain="x.edu.cn").items()}
        failures = {"https://jwc.x.edu.cn/": "证书校验失败（SSL）"}
        from unittest.mock import patch
        with patch.dict(discover.PREFIXES, {"source": ["jwc"]}):
            site["https://www.x.edu.cn/"] = "<title>某某大学</title>"
            entry = discover.discover("某某大学", "x.edu.cn", "source", site.get, why=failures.get)
        self.assertEqual(entry["archives"][0]["first_page"], "http://jwc.x.edu.cn/2821/list.htm")

    def test_office_name_in_logo_alt_counts_but_discipline_office_does_not(self):
        self.assertTrue(discover._office_matches('<title>首页</title><img alt="某某大学教务处" src="logo.png">', "source"))
        self.assertFalse(discover._office_matches('<title>某某大学纪委办公室</title><a>教务处</a>', "source"))

    def test_main_site_section_page_with_other_title_still_leads_to_office(self):
        site = webplus_site(domain="w.edu.cn")
        site["https://www.w.edu.cn/"] = '<title>某某大学</title><a href="/rcpy.htm#md1">本科生教育</a>'
        site["https://www.w.edu.cn/rcpy.htm#md1"] = ('<title>人才培养-某某大学</title>'
                                                    '<a href="https://jwc.w.edu.cn/">本科生院</a>')
        from unittest.mock import patch
        with patch.dict(discover.PREFIXES, {"source": []}):
            entry = discover.discover("某某大学", "w.edu.cn", "source", site.get)
        self.assertEqual(entry["archives"][0]["first_page"], "https://jwc.w.edu.cn/2821/list.htm")

    def test_month_day_only_list_is_reported_as_such(self):
        rows = "".join(f'<li><a href="{i}.htm"><div class="time">09-{20 - i:02d}</div>'
                       f'<div class="title">第{i}条教务通知标题文字</div></a></li>' for i in range(8))
        archive, why = discover._validate_column("http://jwc.m.edu.cn/tztg/index.htm",
                                                 f"<ul>{rows}</ul>" + " " * 3000, lambda u: None)
        self.assertIsNone(archive)
        self.assertIn("没写年份", why)


if __name__ == "__main__":
    unittest.main()

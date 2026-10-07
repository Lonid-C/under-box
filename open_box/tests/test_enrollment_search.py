"""本科就读与推免名单检索的回归（2026-10）。姓名均为测试数据。

覆盖三类问题：
  · 页面读取：robots.txt 取不到/被 WAF 403 不能把整个学校官网判成"禁止抓取"；
    读超时不重试，失败原因写进日志；
  · 检索顺序：推免陈述先发整批名单查询，单个引号词不再"去引号重查"；
  · 关联佐证：其他条目已读到的本校官方名单，补本科学历的「学校」要素。
"""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from app import judge, search
from app.collect import Budget, _read_and_extract
from app.schema import Claim, Evidence
from app.search import PageFetcher, SearchHit, robots_allows


def _resp(status=200, text="", ctype="text/plain"):
    return SimpleNamespace(status_code=status, text=text, content=text.encode(),
                           headers={"content-type": ctype}, raise_for_status=lambda: None)


class RobotsTests(unittest.TestCase):
    def setUp(self):
        search._robots_cache.clear()

    def test_waf_403_on_robots_does_not_block_whole_site(self):
        with patch("httpx.get", return_value=_resp(403, "forbidden")):
            self.assertTrue(robots_allows("https://ugs.school.example.edu/2022/0926/page.htm"))

    def test_robots_timeout_is_treated_as_allowed_and_uses_short_timeout(self):
        seen = {}

        def slow(url, **kw):
            seen.update(kw)
            raise httpx.ConnectTimeout("timeout")

        with patch("httpx.get", side_effect=slow):
            self.assertTrue(robots_allows("https://slow.school.example.edu/a.htm"))
        self.assertLessEqual(seen["timeout"], 5.0)
        self.assertIn("ResumeVerify", seen["headers"]["User-Agent"])

    def test_real_disallow_rule_is_still_respected(self):
        rules = "User-agent: *\nDisallow: /private/\n"
        with patch("httpx.get", return_value=_resp(200, rules)):
            self.assertFalse(robots_allows("https://rules.school.example.edu/private/x.htm"))
            self.assertTrue(robots_allows("https://rules.school.example.edu/info/x.htm"))

    def test_html_served_as_robots_is_not_parsed_as_rules(self):
        with patch("httpx.get", return_value=_resp(200, "<!DOCTYPE html><html>首页</html>",
                                                    "text/html")):
            self.assertTrue(robots_allows("https://spa.school.example.edu/x.htm"))


class FetchFailureTests(unittest.TestCase):
    def test_read_timeout_fails_fast_without_retry(self):
        calls = []

        def hang(url, **kw):
            calls.append(url)
            raise httpx.ReadTimeout("slow")

        fetcher = PageFetcher()
        with patch("app.search.robots_allows", return_value=True), \
             patch("httpx.get", side_effect=hang):
            self.assertIsNone(fetcher.get("https://slow.school.example.edu/notice.htm"))
        self.assertEqual(len(calls), 1)
        self.assertIn("读取超时", fetcher.failures["https://slow.school.example.edu/notice.htm"])

    def test_http_404_is_recorded_without_retry(self):
        calls = []

        def gone(url, **kw):
            calls.append(url)
            return _resp(404, "not found")

        fetcher = PageFetcher()
        with patch("app.search.robots_allows", return_value=True), \
             patch("httpx.get", side_effect=gone):
            self.assertIsNone(fetcher.get("https://school.example.edu/old.pdf"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(fetcher.failures["https://school.example.edu/old.pdf"], "HTTP 404")

    def test_progress_log_names_the_failure_reason(self):
        claim = Claim(id="c", raw_text="2023年保研", raw_locator="t", category="学历",
                      date_label="2023", elements=["学校=示例大学"], entities={"org": "示例大学"})
        fetcher = SimpleNamespace(get=lambda url: None,
                                  failures={"https://a.school.example.edu/x.htm": "读取超时（>12 秒）"})
        lines = []
        _read_and_extract(claim, [SearchHit(url="https://a.school.example.edu/x.htm", title="公示")],
                          fetcher, llm=None, organizer_hosts=set(), budget=Budget(),
                          progress=lines.append)
        self.assertTrue(any("读取超时" in line and "a.school.example.edu" in line for line in lines))


class UndergradChannelTests(unittest.TestCase):
    def test_undergrad_only_uses_official_and_wechat_channels(self):
        from app.collect import collect_for_claim
        from app.strategy import ResumeProfile, build_plan
        claim = Claim(id="u", raw_text="2019.09-2023.06 哈尔滨工程大学 计算机科学与技术",
                      raw_locator="教育", category="学历", date_label="2019-2023",
                      elements=["学校=哈尔滨工程大学", "专业=计算机科学与技术"],
                      entities={"org": "哈尔滨工程大学"})
        plan = build_plan(claim, "张三", ResumeProfile(identity="student"))
        calls = []

        def search(query, site=None):
            calls.append((query, site))
            return []

        collect_for_claim(claim, "张三", SimpleNamespace(search=search), llm=None,
                          queries=plan.queries, budget=Budget(max_searches=8, max_page_reads=0))
        self.assertEqual(calls, [('"张三"', "hrbeu.edu.cn"),
                                 ('"哈尔滨工程大学" "张三"', "mp.weixin.qq.com"),
                                 ("哈尔滨工程大学 张三", "mp.weixin.qq.com")])


def _claim(cid, raw, school, elements, category="学历"):
    return Claim(id=cid, raw_text=raw, raw_locator="教育", category=category,
                 date_label="2019-2023", elements=elements, entities={"org": school})


def _roster(url, snippet, supports, tier="A", signals=("学校一致", "学院一致")):
    ev = Evidence(url=url, title="获得免试攻读2023年硕士学位研究生资格学生名单", publisher="本科生院",
                  source_tier=tier, snippet=snippet, identity_signals=list(signals),
                  supports=list(supports))
    return judge.score_evidence(ev)


class EnrollmentCorroborationTests(unittest.TestCase):
    def setUp(self):
        self.undergrad = _claim("c1", "2019.09-2023.06 哈尔滨工程大学 计算机科学与技术", "哈尔滨工程大学",
                                ["学校=哈尔滨工程大学", "专业=计算机科学与技术"])
        self.grad = _claim("c2", "2023.09 哈尔滨工业大学（保送）", "哈尔滨工业大学",
                           ["学校=哈尔滨工业大学", "入学方式=保送"])

    def _verified(self, grad_evidence):
        return [judge.assess(self.undergrad, [], search_exhausted=True),
                judge.assess(self.grad, grad_evidence)]

    def test_source_school_roster_found_for_push_claim_proves_undergrad_school(self):
        roster = _roster("https://ugs.hrbeu.edu.cn/_upload/article/files/x.pdf",
                         "905 张三 计算机科学与技术学院", ["入学方式=保送"])
        verified = self._verified([roster])
        self.assertEqual(verified[0].status, "none")
        changed = judge.corroborate_enrollment(verified, "张三")
        self.assertEqual(changed, [0])
        self.assertEqual(verified[0].status, "part")
        self.assertEqual(verified[0].proved, ["学校=哈尔滨工程大学"])
        self.assertIn("专业=计算机科学与技术", verified[0].unproved)
        linked = verified[0].evidence[-1]
        self.assertTrue(linked.title.startswith("［关联佐证·来自 c2 学历］"))
        self.assertEqual(linked.supports, ["学校=哈尔滨工程大学"])
        # 原条目的证据不被改写
        self.assertEqual(verified[1].evidence[0].supports, ["入学方式=保送"])

    def test_third_party_list_naming_school_and_person_counts(self):
        contest = _claim("c11", "2021 数学建模国家二等奖", "全国大学生数学建模竞赛组委会",
                         ["奖项=国家二等奖"], category="竞赛")
        ev = Evidence(url="https://www.mcm.edu.cn/upload_cn/node/626/list.pdf",
                      title="2021高教社杯全国大学生数学建模竞赛获奖名单", publisher="组委会",
                      source_tier="A", snippet="本科组二等奖 哈尔滨工程大学 张三 ……",
                      identity_signals=["学校一致", "队友或合作者一致"], supports=["奖项=国家二等奖"])
        judge.score_evidence(ev)
        verified = [judge.assess(self.undergrad, []), judge.assess(contest, [ev])]
        judge.corroborate_enrollment(verified, "张三")
        self.assertIn("学校=哈尔滨工程大学", verified[0].proved)

    def test_list_without_the_candidate_name_is_not_borrowed(self):
        roster = _roster("https://ugs.hrbeu.edu.cn/list.pdf", "905 王某 计算机学院", ["入学方式=保送"])
        verified = self._verified([roster])
        self.assertEqual(judge.corroborate_enrollment(verified, "张三"), [])
        self.assertEqual(verified[0].status, "none")

    def test_identity_unconfirmed_evidence_is_not_borrowed(self):
        roster = _roster("https://ugs.hrbeu.edu.cn/list.pdf", "905 张三", ["入学方式=保送"],
                         signals=())
        verified = self._verified([roster])
        self.assertEqual(judge.corroborate_enrollment(verified, "张三"), [])

    def test_other_school_page_does_not_prove_this_school(self):
        hit_page = _roster("https://hitgs.hit.edu.cn/list.pdf", "仪器学院 张三 总师班",
                           ["学校=哈尔滨工业大学"])
        verified = self._verified([hit_page])
        judge.corroborate_enrollment(verified, "张三")
        self.assertNotIn("学校=哈尔滨工程大学", verified[0].proved)


if __name__ == "__main__":
    unittest.main()


# ── 官网通知栏目按日期定位（app/archive.py）─────────────────────────────────

from datetime import date, timedelta  # noqa: E402

from app.archive import locate_notices, parse_list_page  # noqa: E402

ARCHIVE = {"name": "本科生院·工作通知", "first_page": "https://ugs.school.example.edu.cn/2821/list.htm",
           "list_url": "https://ugs.school.example.edu.cn/2821/list{page}.htm",
           "role": "source", "months": [8, 11], "year_offset": -1}
TARGET_TITLE = "关于公示获得免试攻读2023年硕士学位研究生资格学生名单的通知"
TARGET_URL = "https://ugs.school.example.edu.cn/2022/0926/c2821a297762/page.htm"


def _webplus_site(per_page=15, newest=date(2026, 10, 1), pages=40):
    """仿 WebPlus 栏目：按日期倒序，每 3 天一条，2022-09-26 那条是目标公示。"""
    entries, day = [], newest
    while len(entries) < per_page * pages:
        if day == date(2022, 9, 26):
            entries.append((TARGET_URL, TARGET_TITLE, day))
        else:
            entries.append((f"https://ugs.school.example.edu.cn/{day:%Y/%m%d}/c2821a{len(entries)}/page.htm",
                            f"关于做好第{len(entries)}项教学工作的通知", day))
        day -= timedelta(days=1 if day - timedelta(days=1) >= date(2022, 9, 26) > day - timedelta(days=3)
                         else 3)
    site = {}
    for p in range(pages):
        rows = "".join(f'<li class="news"><span class="news_title"><a href="{u}" title="{t}">{t}</a></span>'
                       f'<span class="news_meta">{d:%Y-%m-%d}</span></li>'
                       for u, t, d in entries[p * per_page:(p + 1) * per_page])
        html = (f'<ul class="news_list">{rows}</ul><div class="pages">'
                f'<em class="all_pages">{pages}</em></div>')
        url = ARCHIVE["first_page"] if p == 0 else ARCHIVE["list_url"].format(page=p + 1)
        site[url] = html
    return site


class ArchiveLocatorTests(unittest.TestCase):
    def test_list_page_parsing_reads_titles_dates_and_page_count(self):
        site = _webplus_site()
        items, total = parse_list_page(site[ARCHIVE["first_page"]], ARCHIVE["first_page"])
        self.assertEqual(total, 40)
        self.assertEqual(len(items), 15)
        self.assertEqual(items[0].date, "2026-10-01")

    def test_binary_search_finds_last_years_qualification_notice_within_eight_reads(self):
        site = _webplus_site()
        calls = []

        def fetch(url):
            calls.append(url)
            return site.get(url)

        hits = locate_notices(ARCHIVE, 2023, fetch)
        self.assertEqual([h.url for h in hits], [TARGET_URL])
        self.assertEqual(hits[0].date, "2022-09-26")
        self.assertLessEqual(len(calls), 8)

    def test_unreadable_first_page_returns_nothing_without_guessing(self):
        self.assertEqual(locate_notices(ARCHIVE, 2023, lambda url: None), [])

    def test_date_is_not_borrowed_from_a_neighbouring_item(self):
        html = ('<ul><li><a href="/a/page.htm">没有日期的栏目说明页面</a></li>'
                '<li><a href="/2022/0926/c1a2/page.htm">关于公示推免资格名单的通知</a>'
                '<span>2022-09-26</span></li></ul>')
        items, _ = parse_list_page(html, "https://ugs.school.example.edu.cn/list.htm")
        self.assertEqual([(i.title, i.date) for i in items], [("关于公示推免资格名单的通知", "2022-09-26")])

    def test_push_claim_reaches_embedded_pdf_roster_through_archive(self):
        from app.collect import collect_for_claim
        claim = Claim(id="g", raw_text="2023.09 某工业大学（保送）", raw_locator="教育", category="学历",
                      date_label="2023.09", date_start="2023-09", elements=["学校=某工业大学", "入学方式=保送"],
                      entities={"org": "某工业大学", "source_school_hint": "某工程大学"})
        site = _webplus_site()
        pdf = "https://ugs.school.example.edu.cn/_upload/article/files/list.pdf"
        site[TARGET_URL] = f'<div class="wp_pdf_player" pdfsrc="{pdf}"></div>'
        site[pdf] = "获得免试攻读2023年硕士学位研究生资格学生名单\n序号 | 姓名 | 学院\n905 | 张三 | 计算机学院"
        fetcher = SimpleNamespace(get=lambda url: site.get(url), failures={})
        seen = []

        def extract(claim, hit, text, llm, **kw):
            seen.append((hit.url, text))
            return None

        with patch("app.collect.archives_for",
                   side_effect=lambda name, role, fetch, **kw: [ARCHIVE] if name == "某工程大学" else []), \
             patch("app.collect.school_domain",
                   side_effect=lambda name: "school.example.edu.cn" if name == "某工程大学" else None), \
             patch("app.collect.extract_evidence", side_effect=extract):
            collect_for_claim(claim, "张三", SimpleNamespace(search=lambda q, site=None: []), llm=None,
                              queries=[], budget=Budget(max_searches=0, max_page_reads=4),
                              fetcher=fetcher)
        self.assertTrue(any(url == pdf and "905 | 张三" in text for url, text in seen))


class WechatAndDeadHostTests(unittest.TestCase):
    def test_robots_blocked_wechat_uses_search_snippet_only_when_name_present(self):
        claim = Claim(id="u", raw_text="2019-2023 哈尔滨工程大学", raw_locator="教育", category="学历",
                      date_label="2019-2023", elements=["学校=哈尔滨工程大学"], entities={"org": "哈尔滨工程大学"})
        with_name = SearchHit(url="https://mp.weixin.qq.com/s?a=1", title="学子风采",
                              snippet="哈尔滨工程大学计算机学院 张三 获得……")
        without = SearchHit(url="https://mp.weixin.qq.com/s?a=2", title="家长进课堂", snippet="活动回顾")
        failures = {with_name.url: "robots.txt 不允许访问", without.url: "robots.txt 不允许访问"}
        fetcher = SimpleNamespace(get=lambda url: None, failures=failures)
        seen, lines = [], []
        with patch("app.collect.extract_evidence",
                   side_effect=lambda c, hit, text, llm, **kw: seen.append((hit, text))):
            _read_and_extract(claim, [with_name, without], fetcher, None, set(),
                              candidate_name="张三", budget=Budget(), progress=lines.append)
        self.assertEqual(len(seen), 1)
        self.assertIn("搜索引擎收录摘要", seen[0][0].title)
        self.assertIn("张三", seen[0][1])
        self.assertTrue(any("只用了搜索摘要" in line for line in lines))

    def test_host_with_connect_timeout_is_skipped_for_the_rest_of_the_run(self):
        calls = []

        def down(url, **kw):
            calls.append(url)
            raise httpx.ConnectTimeout("down")

        fetcher = PageFetcher()
        with patch("app.search.robots_allows", return_value=True), \
             patch("httpx.get", side_effect=down), patch("time.sleep"):
            fetcher.get("https://sec.school.example.edu.cn/a.htm")
            fetcher.get("https://sec.school.example.edu.cn/b.htm")
        self.assertEqual(len(calls), 2)          # 第一页连接重试一次，第二页直接跳过
        self.assertIn("已跳过", fetcher.failures["https://sec.school.example.edu.cn/b.htm"])

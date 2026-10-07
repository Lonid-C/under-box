"""学校官网检索回归：全部使用本地桩，不调用搜索服务或付费模型。"""
import unittest
from unittest.mock import patch

from app import plan, search
from app.collect import Budget, collect_for_claim
from app.schema import Claim
from app.search import SearchHit, ZhipuSearcher
from app.strategy import Query, ResumeProfile, add_admission_source_hints, build_plan


def school_claim(category="奖学金", *, entities=None, elements=None):
    return Claim(
        id="school-search", raw_text="张三在学校获得荣誉", raw_locator="第1页",
        category=category, date_label="2024", entities=entities or {},
        elements=elements or [],
    )


class NoPageReads:
    def get(self, url):
        raise AssertionError("本组只测试搜索调度，不应读取网页")


class NoModelCalls:
    def complete_json(self, *args, **kwargs):
        raise AssertionError("本组只测试搜索调度，不应调用模型")


class RecordingSearcher:
    def __init__(self, respond):
        self.calls = []
        self.respond = respond

    def search(self, query, site=None):
        self.calls.append((query, site))
        return self.respond(query, site)


class SchoolResolutionTests(unittest.TestCase):
    def test_renamed_joint_university_keeps_full_name_and_current_official_domain(self):
        school = "北京师范大学-香港浸会大学联合国际学院"
        self.assertEqual(plan.school_name(school), school)
        self.assertEqual(plan.school_domain(school), "bnbu.edu.cn")
        self.assertEqual(plan.school_name("北师香港浸会大学数据科学学院"), school)
        self.assertEqual(plan.school_domain("北师港浸大"), "bnbu.edu.cn")
        claim = school_claim("学历", entities={"org": school, "major": "数据科学"},
                             elements=[f"学校={school}", "专业=数据科学"])
        first = build_plan(claim, "张三").queries[0]
        self.assertEqual(first.site, "bnbu.edu.cn")
        self.assertEqual(first.text, '"张三"')

    def test_alias_with_department_resolves_to_official_school(self):
        self.assertEqual(plan.school_name("北大计算机学院学生会"), "北京大学")
        self.assertEqual(plan.school_domain("北大计算机学院学生会"), "pku.edu.cn")

    def test_unknown_school_keeps_its_name_without_guessing_a_domain(self):
        self.assertEqual(plan.school_name("新校大学计算机学院"), "新校大学")
        self.assertIsNone(plan.school_domain("新校大学计算机学院"))

    def test_ambiguous_alias_and_company_do_not_force_a_school_domain(self):
        self.assertIsNone(plan.school_domain("交大"))
        self.assertIsNone(plan.school_domain("示例科技有限公司"))
        self.assertEqual(plan.school_name("示例科技有限公司"), "")

    def test_explicit_school_precedes_event_organizer(self):
        for field in ("school", "university"):
            with self.subTest(field=field):
                c = school_claim(
                    category="竞赛", entities={field: "北大计算机学院", "org": "示例赛事组委会"},
                    elements=["学校=清华大学", "赛事=示例赛事"],
                )
                self.assertEqual(plan.claim_school(c), "北京大学")

    def test_school_element_precedes_other_organization(self):
        c = school_claim(
            entities={"org": "示例基金会"}, elements=["学校=清华大学", "授予单位=示例基金会"],
        )
        self.assertEqual(plan.claim_school(c), "清华大学")

    def test_student_organization_element_can_supply_school(self):
        c = school_claim(category="学生工作", elements=["任职组织=北大计算机学院学生会"])
        self.assertEqual(plan.claim_school(c), "北京大学")
        self.assertEqual(plan.claim_school(school_claim(entities={"org": "示例有限公司"})), "")


class OfficialPlanTests(unittest.TestCase):
    def test_push_admission_queries_both_schools_without_summer_camp_noise(self):
        undergraduate = Claim(
            id="undergrad", raw_text="2019.09—2023.06 哈尔滨工程大学 计算机科学与技术",
            raw_locator="教育经历", category="学历", date_label="2019.09—2023.06",
            date_start="2019-09", date_end=None,
            elements=["学校=哈尔滨工程大学", "专业=计算机科学与技术"],
            entities={"org": "哈尔滨工程大学", "major": "计算机科学与技术"},
        )
        graduate = Claim(
            id="graduate", raw_text="2023.09 哈尔滨工业大学（保送） 仪器科学与技术",
            raw_locator="教育经历", category="学历", date_label="2023.09",
            date_start="2023-09", elements=["学校=哈尔滨工业大学", "入学方式=保送"],
            entities={"org": "哈尔滨工业大学", "major": "仪器科学与技术"},
        )
        enriched = add_admission_source_hints([undergraduate, graduate])
        self.assertEqual(enriched[1].entities["source_school_hint"], "哈尔滨工程大学")
        self.assertEqual(enriched[1].entities["source_field_hint"], "计算机科学与技术")
        self.assertEqual(graduate.entities, {"org": "哈尔滨工业大学", "major": "仪器科学与技术"})
        queries = build_plan(enriched[1], "李四", ResumeProfile(identity="student")).queries
        # 推免陈述用类别的 exception_budget（10 次），整批名单先于姓名探针。
        self.assertEqual(len(queries), 10)
        self.assertEqual(queries[0].site, "hrbeu.edu.cn")
        self.assertEqual(queries[0].text, "免试攻读2023年硕士学位研究生 资格")
        name_probe = next(i for i, q in enumerate(queries)
                          if q.site == "hit.edu.cn" and q.text == '"李四"')
        roster = next(i for i, q in enumerate(queries)
                      if q.site == "hit.edu.cn" and q.text == "2023 推免 拟录取 名单")
        self.assertLess(roster, name_probe)
        self.assertTrue(any(q.site == "mp.weixin.qq.com" and q.text ==
                            '"哈尔滨工业大学" "李四"' for q in queries))
        self.assertTrue(any(q.site == "hrbeu.edu.cn" and q.text ==
                            "2023 免试 资格 名单" for q in queries))
        self.assertTrue(any(q.site == "hrbeu.edu.cn" and q.text == '"李四" 推免'
                            for q in queries))
        self.assertTrue(any(q.site == "hrbeu.edu.cn" and q.text ==
                            "2023 计算机科学与技术 免试 名单" for q in queries))
        self.assertTrue(any(q.site == "hrbeu.edu.cn" and q.text ==
                            "2022 推免 公示" for q in queries))
        self.assertTrue(any(q.site == "hit.edu.cn" and q.text ==
                            "2023 推免 拟录取 名单" for q in queries))
        self.assertTrue(any(q.site == "hit.edu.cn" and q.text ==
                            "2023 硕士 拟录取 名单" for q in queries))
        self.assertFalse(any("夏令营" in q.text for q in queries))

    def test_summer_camp_does_not_trigger_admission_rosters(self):
        camp = Claim(
            id="camp", raw_text="2021年 清华大学夏令营优秀营员", raw_locator="教育经历",
            category="学历", date_label="2021", date_start="2021-06",
            elements=["学校=清华大学", "荣誉=优秀营员"], entities={"org": "清华大学"},
        )
        queries = build_plan(camp, "张三", ResumeProfile(identity="student")).queries
        self.assertTrue(any("夏令营" in q.text for q in queries))
        self.assertFalse(any("推免" in q.text or "推荐免试" in q.text for q in queries))

    def test_exam_admission_uses_exam_rosters_not_push_rosters(self):
        exam = Claim(id="exam", raw_text="2023年考研进入哈尔滨工业大学仪器科学与技术专业",
                     raw_locator="教育经历", category="学历", date_label="2023",
                     date_start="2023-09", elements=["学校=哈尔滨工业大学", "入学方式=统考"],
                     entities={"org": "哈尔滨工业大学", "major": "仪器科学与技术"})
        queries = build_plan(exam, "张三", ResumeProfile(identity="student")).queries
        self.assertTrue(any(q.site == "hit.edu.cn" and q.text == '"张三" 统考'
                            for q in queries))
        self.assertTrue(any("统考 拟录取 名单" in q.text for q in queries))
        self.assertFalse(any("推免" in q.text or "推荐免试" in q.text for q in queries))

    def test_ambiguous_undergraduate_school_does_not_infer_source(self):
        graduate = Claim(id="graduate", raw_text="2023年保送至哈尔滨工业大学",
                         raw_locator="教育经历", category="学历", date_label="2023",
                         date_start="2023-09", entities={"org": "哈尔滨工业大学"})
        priors = [Claim(id=f"prior-{i}", raw_text=f"{school}本科学习",
                        raw_locator="教育经历", category="学历", date_label="2019-2023",
                        date_start="2019-09", date_end="2023-06", entities={"org": school})
                  for i, school in enumerate(("哈尔滨工程大学", "清华大学"))]
        self.assertNotIn("source_school_hint",
                         add_admission_source_hints([*priors, graduate])[-1].entities)

    def test_source_college_precedes_major_in_notice_query(self):
        undergraduate = Claim(id="u", raw_text="2019-2023 哈尔滨工程大学",
                              raw_locator="简历", category="学历", date_label="2019-2023",
                              date_start="2019-09", date_end="2023-06",
                              entities={"org": "哈尔滨工程大学",
                                        "dept": "计算机科学与技术学院", "major": "软件工程"})
        graduate = Claim(id="g", raw_text="2023年保送至哈尔滨工业大学",
                         raw_locator="简历", category="学历", date_label="2023",
                         date_start="2023-09", entities={"org": "哈尔滨工业大学"})
        enriched = add_admission_source_hints([undergraduate, graduate])[1]
        self.assertEqual(enriched.entities["source_field_hint"], "计算机科学与技术学院")

    def test_school_facts_start_with_short_official_search(self):
        cases = [
            ("学历", {}, None),
            ("校内荣誉", {"award": "优秀学生"}, "优秀学生"),
            ("奖学金", {"award": "国家奖学金"}, "国家奖学金"),
            ("学生工作", {"role": "科技部部长"}, "科技部部长"),
            ("竞赛", {"contest": "示例竞赛"}, "示例竞赛"),
        ]
        for category, extra, keyword in cases:
            with self.subTest(category=category):
                c = school_claim(category, entities={"org": "北京大学", **extra})
                result = build_plan(c, "张三", ResumeProfile(identity="student", level="entry"))
                self.assertTrue(result.queries)
                first = result.queries[0]
                self.assertTrue(first.source.startswith("school:"), first)
                self.assertEqual(first.site, "pku.edu.cn")
                self.assertIn("张三", first.text)
                if keyword:
                    self.assertIn(keyword, first.text)

    def test_regular_education_retains_official_and_wechat_channels(self):
        c = school_claim("学历", entities={"org": "北京大学", "major": "计算机科学"})
        result = build_plan(c, "张三")
        self.assertIn("pku.edu.cn", [q.site for q in result.queries])
        self.assertIn("mp.weixin.qq.com", [q.site for q in result.queries])

    def test_unknown_school_is_deferred_to_discovery(self):
        c = school_claim(entities={"org": "新校大学计算机学院", "award": "国家奖学金"})
        first = build_plan(c, "张三").queries[0]
        self.assertTrue(first.source.startswith("school:"))
        self.assertEqual(first.site, "school")

    def test_event_organizer_is_not_treated_as_school(self):
        c = school_claim("竞赛", entities={"org": "示例竞赛组委会", "contest": "示例竞赛"})
        self.assertFalse(any(q.source.startswith("school:") for q in build_plan(c, "张三").queries))


class OfficialCollectionTests(unittest.TestCase):
    def collect(self, c, query, searcher, limit):
        budget = Budget(max_searches=limit, max_page_reads=0, max_seconds=30)
        with patch("app.collect.github_hits", return_value=[]):
            collect_for_claim(
                c, "张三", searcher, NoModelCalls(), queries=[query],
                fetcher=NoPageReads(), budget=budget,
            )
        return budget

    def test_unknown_school_discovery_is_followed_by_scoped_fact_search(self):
        def respond(query, site):
            if site is None:
                return [SearchHit("https://www.xinxiao.edu.cn/", title="新校大学", publisher="新校大学")]
            return [SearchHit("https://xsc.xinxiao.edu.cn/notice/1", title="张三 国家奖学金")]

        searcher = RecordingSearcher(respond)
        c = school_claim(entities={"org": "新校大学计算机学院"})
        q = Query('"张三" "国家奖学金"', site="school", source="school:official")
        budget = self.collect(c, q, searcher, 2)
        self.assertEqual(len(searcher.calls), 2)
        self.assertIn("新校大学", searcher.calls[0][0])
        self.assertIn("官网", searcher.calls[0][0])
        self.assertIsNone(searcher.calls[0][1])
        self.assertEqual(searcher.calls[1], (q.text, "xinxiao.edu.cn"))
        self.assertEqual(budget.searches, 2, "官网发现也必须计入搜索预算")

    def test_confirmed_domain_can_be_reused_by_the_next_claim(self):
        cache = {"新校大学": "xinxiao.edu.cn"}
        searcher = RecordingSearcher(lambda query, site: [
            SearchHit("https://xsc.xinxiao.edu.cn/notice/2", title="张三 获奖")])
        c = school_claim(entities={"org": "新校大学计算机学院"})
        q = Query('"张三" 获奖', site="school", source="school:official")
        budget = Budget(max_searches=1, max_page_reads=0, max_seconds=30)
        with patch("app.collect.github_hits", return_value=[]):
            collect_for_claim(c, "张三", searcher, NoModelCalls(), queries=[q],
                              fetcher=NoPageReads(), budget=budget, school_domains=cache)
        self.assertEqual(searcher.calls, [(q.text, "xinxiao.edu.cn")])

    def test_discovery_cannot_overrun_the_search_budget(self):
        searcher = RecordingSearcher(lambda query, site: [
            SearchHit("https://www.xinxiao.edu.cn/", title="新校大学", publisher="新校大学")])
        c = school_claim(entities={"org": "新校大学"})
        q = Query('"张三"', site="school", source="school:official")
        budget = self.collect(c, q, searcher, 1)
        self.assertEqual(len(searcher.calls), 1)
        self.assertIn("官网", searcher.calls[0][0])
        self.assertEqual(budget.searches, 1)

    def test_failed_discovery_does_not_issue_an_unscoped_person_query(self):
        searcher = RecordingSearcher(lambda query, site: [])
        c = school_claim(entities={"org": "新校大学"})
        q = Query('"张三" "国家奖学金"', site="school", source="school:official")
        self.collect(c, q, searcher, 3)
        self.assertTrue(searcher.calls)
        self.assertTrue(all("新校大学" in query for query, site in searcher.calls if site is None))

    def test_empty_official_query_removes_quotes_before_widening_domain(self):
        searcher = RecordingSearcher(lambda query, site: [])
        c = school_claim(entities={"org": "北京大学", "award": "国家奖学金"})
        q = Query('"张三" "国家奖学金"', site="pku.edu.cn", source="school:official")
        budget = self.collect(c, q, searcher, 3)
        self.assertEqual(len(searcher.calls), 3)
        self.assertEqual(searcher.calls[0], (q.text, "pku.edu.cn"))
        relaxed, site = searcher.calls[1]
        self.assertEqual(site, "pku.edu.cn")
        self.assertNotIn('"', relaxed)
        self.assertIn("张三", relaxed)
        self.assertIn("国家奖学金", relaxed)
        widened, site = searcher.calls[2]
        self.assertIsNone(site)
        for term in ("北京大学", "张三", "国家奖学金"):
            self.assertIn(term, widened)
        self.assertEqual(budget.searches, 3)


class DomainFilterTests(unittest.TestCase):
    def test_domain_matching_allows_departments_and_rejects_lookalikes(self):
        accepted = ["https://pku.edu.cn/", "https://cs.pku.edu.cn/news/1", "https://WWW.PKU.EDU.CN/"]
        rejected = [
            "https://notpku.edu.cn/", "https://pku.edu.cn.attacker.example/",
            "https://pku.edu.cn@attacker.example/", "https://other.edu.cn/?site=pku.edu.cn",
            "/relative-notice",
        ]
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(search.url_in_domain(url, "pku.edu.cn"))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(search.url_in_domain(url, "pku.edu.cn"))

    def test_glm_enforces_official_domain_even_if_provider_returns_noise(self):
        import httpx
        endpoint = "https://search.example/query"
        official = "https://xsc.pku.edu.cn/notices/1"
        response = httpx.Response(200, request=httpx.Request("POST", endpoint), json={
            "search_result": [
                {"link": official, "title": "名单"},
                {"link": "https://other.edu.cn/notices/1", "title": "北京大学相关新闻"},
                {"link": "https://pku.edu.cn.attacker.example/notices/1", "title": "冒牌站"},
            ],
        })
        client = ZhipuSearcher(api_key="offline-test-key", endpoint=endpoint, engine="search_pro")
        with patch("httpx.post", return_value=response) as request, patch.object(client, "_throttle"):
            hits = client.search("张三 奖学金", site="pku.edu.cn")
        self.assertEqual([hit.url for hit in hits], [official])
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["search_domain_filter"], "pku.edu.cn")
        self.assertIs(body["search_intent"], False)

    def test_quark_configuration_uses_domain_capable_engine_for_official_queries(self):
        import httpx
        endpoint = "https://search.example/query"
        response = httpx.Response(200, request=httpx.Request("POST", endpoint), json={"search_result": []})
        client = ZhipuSearcher(api_key="offline-test-key", endpoint=endpoint, engine="search_pro_quark")
        with patch("httpx.post", return_value=response) as request, patch.object(client, "_throttle"):
            client.search("张三", site="pku.edu.cn")
        self.assertEqual(request.call_args.kwargs["json"]["search_engine"], "search_pro")
        self.assertEqual(request.call_args.kwargs["json"]["search_domain_filter"], "pku.edu.cn")


if __name__ == "__main__":
    unittest.main()

"""Specialist routing, bounded search and provenance-based collaboration."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import judge, pipeline
from app.collect import Budget
from app.internship import build_manual_review, company_name, public_phones, firm_url
from app.schema import Claim, Evidence, VerifiedClaim
from app.search import SearchHit
from app.strategy import Plan, Query, ResumeProfile, build_plan
from app.verification_agents import agent_for, VerificationCoordinator, employment_supports

COMPANY = "合成蓝桥科技有限公司"
URL = "https://www.qcc.com/firm/synthetic_company.html"


def internship(company=COMPANY):
    return Claim(id="intern", category="实习", raw_text=f"2024年在{company}实习，开发岗位",
                 raw_locator="测试", date_label="2024", entities={"company": company},
                 elements=[f"单位={company}", "岗位=开发", "起止时间=2024年"])


def page(company=COMPANY, phone="010-12345678"):
    return f"<html><title>{company} - 企查查</title><h1>{company}</h1><div>电话：{phone}</div><footer>企查查客服电话：400-111-2222</footer></html>"


class FakeSearcher:
    def __init__(self, hits): self.hits, self.calls = hits, []
    def search(self, query, site=None):
        self.calls.append((query, site))
        return self.hits


class FakeFetcher:
    def __init__(self, html): self.html, self.calls, self.resolved_urls = html, [], {}
    def get(self, url): self.calls.append(url); return self.html


class InternshipTests(unittest.TestCase):
    def test_company_contacts_never_prove_candidate_internship(self):
        c = internship()
        searcher, fetcher = FakeSearcher([SearchHit(URL, COMPANY + " - 企查查")]), FakeFetcher(page())
        vc = build_manual_review(c, searcher, fetcher, Budget())
        self.assertEqual(vc.status, "none"); self.assertTrue(vc.needs_human)
        self.assertEqual(vc.proved, []); self.assertEqual(vc.evidence, [])
        self.assertEqual(vc.unproved, c.elements)
        self.assertEqual(vc.manual_review["state"], "contact_found")
        self.assertEqual(vc.manual_review["companyUrl"], URL)
        self.assertEqual(vc.manual_review["contacts"][0]["phone"], "010-12345678")
        self.assertEqual(len(vc.manual_review["contacts"]), 1)
        self.assertEqual(searcher.calls, [('"' + COMPANY + '"', "qcc.com")])

    def test_blocked_page_preserves_link_and_does_not_guess_snippet_phone(self):
        hit = SearchHit(URL, COMPANY + " - 企查查", snippet="联系电话：010-12345678")
        vc = build_manual_review(internship(), FakeSearcher([hit]), FakeFetcher(None), Budget())
        self.assertIn("qcc.com/web/search?key=", vc.manual_review["queryUrl"])
        self.assertEqual(vc.manual_review["contacts"], [])
        self.assertEqual(vc.manual_review["companyUrl"], "")

    def test_wrong_company_parent_brand_and_related_party_are_not_contact_sources(self):
        for other in ("合成蓝桥科技有限公司北京分公司", "合成蓝桥集团有限公司", "另一家公司"):
            self.assertEqual(public_phones(page(other), COMPANY), [])
        c = internship("蓝桥")
        vc = build_manual_review(c, FakeSearcher([SearchHit(URL, COMPANY + " - 企查查")]), FakeFetcher(page()), Budget())
        self.assertEqual(vc.manual_review["contacts"], [])

    def test_masked_number_and_site_customer_service_are_not_used(self):
        for phone in ("010-1234****", "138****1234", "400-***-2222"):
            self.assertEqual(public_phones(page(phone=phone), COMPANY), [])
        html = f"<h1>{COMPANY}</h1><p>企查查客服电话：400-111-2222</p><script>电话：13812345678</script>"
        self.assertEqual(public_phones(html, COMPANY), [])

    def test_company_field_must_occur_in_input(self):
        c = internship(); c.entities["company"] = "模型猜测公司"
        self.assertEqual(company_name(c), COMPANY)
        c.elements = []
        self.assertEqual(company_name(c), "")
        searcher = FakeSearcher([])
        vc = build_manual_review(c, searcher, FakeFetcher(page()), Budget())
        self.assertEqual(searcher.calls, [])
        self.assertEqual(vc.manual_review["state"], "company_name_missing")

    def test_only_qcc_company_pages_and_confirmed_redirects_are_allowed(self):
        for url in ("https://www.qcc.com.evil.test/firm/a.html", "http://www.qcc.com/firm/a.html",
                    "https://user@www.qcc.com/firm/a.html", "https://www.qcc.com/web/search?key=x"):
            self.assertFalse(firm_url(url))
        fetcher = FakeFetcher(page()); fetcher.resolved_urls[URL] = "https://www.qcc.com/web/cms/overseaApply"
        vc = build_manual_review(internship(), FakeSearcher([SearchHit(URL, COMPANY)]), fetcher, Budget())
        self.assertEqual(vc.manual_review["contacts"], [])

    def test_query_and_read_budget_caps_are_respected(self):
        searcher, fetcher = FakeSearcher([SearchHit(URL, COMPANY)]), FakeFetcher(page())
        vc = build_manual_review(internship(), searcher, fetcher, Budget(max_searches=0))
        self.assertEqual(searcher.calls, []); self.assertTrue(vc.search_exhausted)
        budget = Budget(max_page_reads=0)
        vc = build_manual_review(internship(), searcher, fetcher, budget)
        self.assertEqual(fetcher.calls, []); self.assertTrue(vc.search_exhausted)
        self.assertEqual(budget.searches, 1)

    def test_search_failure_still_returns_manual_entry(self):
        searcher = SimpleNamespace(search=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("temporary failure")))
        vc = build_manual_review(internship(), searcher, FakeFetcher(page()), Budget())
        self.assertEqual(vc.status, "none"); self.assertTrue(vc.manual_review["queryUrl"])
        self.assertTrue(vc.manual_review["limitations"])

    def test_internship_plan_is_company_only_and_default_not_skipped(self):
        plan = build_plan(internship(), "合成张三", ResumeProfile(identity="student"))
        self.assertEqual(plan.profile_id, "internship_manual")
        self.assertEqual(plan.budget["searches"], 1)
        self.assertEqual(plan.queries[0].site, "qcc.com")
        self.assertNotIn("合成张三", plan.queries[0].text)
        with patch.dict(os.environ, {}, clear=True): self.assertEqual(pipeline.skip_categories(), set())
        with patch.dict(os.environ, {"SKIP_CATEGORIES":"实习"}): self.assertEqual(pipeline.skip_categories(), {"实习"})

    def test_pipeline_internship_uses_no_person_evidence_or_extraction_model(self):
        c = internship(); logs = []
        doc = SimpleNamespace(kind="md", source="synthetic", safe_text=c.raw_text, risks=[])
        prepared = pipeline.PreparedResume("synthetic", doc, object(), ResumeProfile(identity="student"), [c], "合成张三")
        with patch.object(pipeline, "collect_for_claim", side_effect=AssertionError("No candidate search")), \
             patch.object(pipeline, "generate_question", side_effect=AssertionError("No extra model call")):
            report = pipeline.verify_prepared(prepared, claim_ids=["intern"],
                searcher=FakeSearcher([SearchHit(URL, COMPANY)]), fetcher=FakeFetcher(page()), progress=logs.append)
        self.assertEqual(len(report.claims), 1)
        self.assertEqual(report.claims[0].verification_agent["id"], "internship")
        self.assertTrue(report.claims[0].manual_review["contacts"])


class SpecialistTests(unittest.TestCase):
    def test_all_categories_have_a_specific_agent(self):
        from typing import get_args
        from app.schema import Category
        for category in get_args(Category):
            self.assertIn(category, agent_for(Claim(id="x",category=category,raw_text="测试",raw_locator="",date_label="")).categories)

    def test_employment_search_adds_input_org_and_role_without_growing_budget(self):
        c = Claim(id="work",category="任职",raw_text="合成张三在合成大学担任工程师",raw_locator="",date_label="",entities={"org":"合成大学","role":"工程师"})
        plan = Plan([Query('"合成张三"')], "test", "test", budget={"searches":1,"reads":1,"seconds":10})
        result = agent_for(c).refine_plan(c, "合成张三", plan)
        self.assertIn("合成大学",result.queries[0].text); self.assertIn("工程师",result.queries[0].text)
        self.assertEqual(result.budget["searches"],1)

    def test_attendance_at_company_is_not_employment(self):
        c = Claim(id="w",category="任职",raw_text=f"在{COMPANY}任工程师",raw_locator="",date_label="",entities={"company":COMPANY},elements=[f"单位={COMPANY}","岗位=工程师"])
        ev = Evidence(url="https://independent.example/news",title="活动记录",publisher="",source_tier="B",snippet=f"张三参加{COMPANY}的合作活动。",supports=c.elements)
        self.assertEqual(employment_supports(c, ev), [])
        self.assertEqual(judge.assess(c, [ev]).evidence, [])
        ev.snippet = f"张三在{COMPANY}任工程师。"
        self.assertEqual(employment_supports(c, ev), c.elements)

    def test_employment_dates_not_borrowed_from_publish_date(self):
        c = Claim(id="w",category="任职",raw_text=f"2024年在{COMPANY}任工程师",raw_locator="",date_label="2024",entities={"company":COMPANY},elements=[f"单位={COMPANY}","起止时间=2024年"])
        ev = Evidence(url="https://independent.example/news",title="2024年新闻",publisher="",source_tier="B",snippet=f"张三在{COMPANY}任工程师。",published_at="2024-01-01",supports=c.elements)
        self.assertEqual(employment_supports(c, ev), [c.elements[0]])
        ev.snippet=f"2024年，{COMPANY}工程师张三参加行业会议。"
        self.assertEqual(employment_supports(c, ev), [c.elements[0]])
        ev.snippet=f"张三于2024年入职{COMPANY}任工程师。"
        self.assertEqual(employment_supports(c, ev), c.elements)

    def test_coordinator_shares_only_proved_elements_with_direct_attributed_sources(self):
        from app.provenance import bind_page
        from app.schema import ElementProof
        c = Claim(id="edu",category="学历",raw_text="合成大学本科",raw_locator="",date_label="",elements=["学校=合成大学","学位=学士"],entities={"org":"合成大学"})
        ev = bind_page(Evidence(url="https://school.example/roster",title="本人名单",publisher="",source_tier="A",snippet="合成张三 合成大学本科",identity_score=2,identity_signals=["学校一致"],supports=[c.elements[0]]),"合成张三 合成大学本科")
        vc = VerifiedClaim(claim=c,status="part",proved=[c.elements[0]],unproved=[c.elements[1]],evidence=[ev],
                           element_proofs=[ElementProof(element=c.elements[0],state="verified",evidence_ids=[ev.evidence_id],accepted_tier="A")])
        coordinator = VerificationCoordinator(); coordinator.record([vc])
        next_claim = coordinator.context_for(internship())
        anchors = next_claim.entities["verified_identity_anchors"]
        self.assertEqual([f["element"] for f in anchors], [c.elements[0]])
        self.assertEqual(anchors[0]["urls"], [ev.url]); self.assertEqual(anchors[0]["stage"],"本科")
        anchors[0]["urls"].append("corrupt")
        self.assertNotIn("corrupt",coordinator.context_for(c).entities["verified_identity_anchors"][0]["urls"])
        self.assertEqual(VerificationCoordinator().context_for(c).entities["verified_identity_anchors"], [])

    def test_contact_hints_and_unread_snippets_cannot_be_shared_as_proof(self):
        c = internship(); coordinator = VerificationCoordinator()
        ev = Evidence(url=URL,title="公司",publisher="",source_tier="C",snippet="公司存在",identity_score=5,supports=c.elements)
        vc = VerifiedClaim(claim=c,status="none",proved=c.elements,evidence=[ev])
        coordinator.record([vc]); self.assertEqual(coordinator.context_for(c).entities["verified_identity_anchors"], [])
        c.category="任职"; ev.extraction_method="search_snippet"
        coordinator.record([vc]); self.assertEqual(coordinator.context_for(c).entities["verified_identity_anchors"], [])

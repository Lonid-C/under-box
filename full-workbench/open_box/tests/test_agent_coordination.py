"""Executable specialist/source contracts and cross-category cooperation."""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import pipeline
from app.collect import Budget, collect_for_claim
from app.data_sources import (AgentRun, SOURCES, SourceNotAllowed, SourceFetcher, SourceSearcher,
                              invoke_source, safe_reference)
from app.schema import Claim, Evidence, VerifiedClaim
from app.strategy import Plan, Query, ResumeProfile
from app.usage import UsageExecutor
from app.verification_agents import agent_for, VerificationCoordinator


def claim(category="论文", ident="p", raw="张三发表 Synthetic study", **entities):
    return Claim(id=ident,category=category,raw_text=raw,raw_locator="合成测试",date_label="",
                 entities=entities)


def run_for(c, queries=None):
    plan=Plan(queries or [], "synthetic", "合成策略", budget={"searches":2,"reads":2,"seconds":20})
    return AgentRun(agent_for(c),c,plan,Budget(max_searches=2,max_page_reads=2,max_seconds=20))


class SourceContractTests(unittest.TestCase):
    def test_publication_types_use_distinct_specialists_and_tools(self):
        expected={"论文":("paper","paper_metadata"),"著作":("book","book_catalogs"),
                  "专利":("patent","patent_records"),"会议":("conference","conference_records"),
                  "竞赛":("competition","competition_records"),"奖学金":("award","school_records")}
        for category,(ident,tool) in expected.items():
            agent=agent_for(claim(category))
            self.assertEqual(agent.id,ident);self.assertIn(tool,agent.source_ids)
            self.assertIn(tool,[t["id"] for t in agent.describe()["tools"]])

    def test_private_database_access_is_not_claimed(self):
        for source in SOURCES.values():
            self.assertFalse(source.describe()["automaticPrivateAccess"])
        self.assertEqual(SOURCES["school_catalog"].role,"navigation_only")
        self.assertEqual(SOURCES["competition_catalog"].role,"navigation_only")
        self.assertEqual(SOURCES["qcc_contacts"].role,"manual_review_only")

    def test_wrong_category_source_is_rejected_before_adapter_runs(self):
        work=claim("任职",raw="张三在合成公司任工程师");called=[]
        run=run_for(work)
        with run.bind(),self.assertRaises(SourceNotAllowed):
            invoke_source("paper_metadata",lambda:called.append("wrong"))
        self.assertEqual(called,[])
        with self.assertRaises(SourceNotAllowed):invoke_source("unknown",lambda:called.append("wrong"))
        self.assertEqual(called,[])

    def test_unrelated_query_kinds_and_unprovided_github_are_removed(self):
        work=claim("任职")
        p=Plan([Query("private",kind="crossref"),Query("role")],"test","test",budget={"searches":2})
        agent_for(work).refine_plan(work,"张三",p)
        self.assertEqual([q.kind for q in p.queries],["web"]);self.assertTrue(p.dropped)
        c=claim("项目",raw="张三开发普通项目")
        p=Plan([Query("guessed",kind="github")],"test","test",budget={"searches":2})
        agent_for(c).refine_plan(c,"张三",p)
        self.assertEqual(p.queries,[])

    def test_source_adapter_errors_do_not_leak_key_or_raw_provider_message(self):
        c=claim();run=run_for(c)
        secret="synthetic-secret-never-log"
        def fail():raise RuntimeError("key="+secret)
        with run.bind(),self.assertRaises(RuntimeError):
            invoke_source("paper_metadata",fail,reference="https://example.org/r?api_key="+secret)
        events=run.snapshot()["events"]
        self.assertEqual(events[0]["state"],"failed")
        self.assertEqual(events[0]["error"],"RuntimeError")
        self.assertNotIn(secret,str(events))
        self.assertNotIn("password",safe_reference("https://user:password@example.org/r"))

    def test_each_concurrent_agent_and_nested_tool_keeps_own_trace(self):
        paper,work=run_for(claim()),run_for(claim("任职",ident="w"))
        barrier=threading.Barrier(2)
        def execute(run,source):
            with run.bind():
                barrier.wait(timeout=4)
                with UsageExecutor(max_workers=1) as nested:
                    return nested.submit(invoke_source,source,lambda:["record"],reference=run.claim.id).result()
        with UsageExecutor(max_workers=2) as pool:
            a=pool.submit(execute,paper,"paper_metadata");b=pool.submit(execute,work,"public_web")
            self.assertEqual(a.result(),["record"]);self.assertEqual(b.result(),["record"])
        self.assertEqual(paper.snapshot()["events"][0]["sourceId"],"paper_metadata")
        self.assertEqual(work.snapshot()["events"][0]["sourceId"],"public_web")
        invoke_source("public_web",lambda:[],reference="outside")
        self.assertEqual(len(work.snapshot()["events"]),1)

    def test_failed_read_and_empty_search_are_observable_without_becoming_proof(self):
        c=claim("学历",raw="张三合成大学本科");run=run_for(c,[Query('"张三"',site="school")])
        search=SourceSearcher(SimpleNamespace(search=lambda *a,**k:[]),run)
        fetch=SourceFetcher(SimpleNamespace(get=lambda u:None),run)
        with run.bind():
            self.assertEqual(search.search('"张三"',site="synthetic.example"),[])
            self.assertIsNone(fetch.get("https://synthetic.example/record"))
        self.assertEqual([e["state"] for e in run.snapshot()["events"]],["empty","empty"])
        self.assertTrue(all(e["sourceId"]=="school_records" for e in run.snapshot()["events"]))
        self.assertEqual(c.elements,[])

    def test_read_restriction_is_not_recorded_as_an_empty_search(self):
        c=claim("学历");run=run_for(c);url="https://school.example/record"
        fetcher=SourceFetcher(SimpleNamespace(get=lambda u:None,failures={url:"HTTP 403"}),run)
        with run.bind():self.assertIsNone(fetcher.get(url))
        event=run.snapshot()["events"][0]
        self.assertEqual(event["state"],"failed");self.assertEqual(event["error"],"ReadUnavailable")
        self.assertEqual(event["reason"],"HTTP 403")

    def test_paper_database_dispatch_is_actual_and_uses_no_paid_search(self):
        c=claim(title="Synthetic study");c.elements=["论文标题=Synthetic study"]
        q=Query("Synthetic study",kind="crossref")
        run=run_for(c,[q]);hits=[]
        search=SimpleNamespace(search=lambda *a,**k:(_ for _ in ()).throw(AssertionError("paid web search")))
        with patch("app.collect.paper_hits",return_value=hits) as adapter,run.bind():
            collect_for_claim(c,"张三",search,object(),fetcher=object(),
                              budget=Budget(max_searches=1,max_page_reads=0),queries=[q])
        adapter.assert_called_once()
        event=run.snapshot()["events"][0]
        self.assertEqual(event["sourceId"],"paper_metadata");self.assertEqual(event["state"],"empty")

    def test_report_trace_is_bounded_and_task_duration_freezes_when_done(self):
        c=claim();run=run_for(c)
        with run.bind():
            for _ in range(85):invoke_source("paper_metadata",lambda:[],reference="test")
        self.assertEqual(len(run.snapshot()["events"]),80);self.assertTrue(run.snapshot()["truncated"])
        run.finish(VerifiedClaim(claim=c,status="none"))
        elapsed=run.snapshot()["budget"]["seconds"]
        run.budget.started-=100
        self.assertEqual(run.snapshot()["budget"]["seconds"],elapsed)


class CollaborationTests(unittest.TestCase):
    def test_selected_dependency_batches_keep_education_order_and_parallel_independence(self):
        bs=claim("学历",ident="bs",raw="张三合成大学本科")
        ms=claim("学历",ident="ms",raw="张三合成大学硕士")
        p=claim();w=claim("任职",ident="w");a=claim("奖学金",ident="a")
        groups=VerificationCoordinator().schedule([w,a,ms,p,bs])
        self.assertEqual([[c.id for _,c in group] for _,group in groups],[["bs"],["ms"],["a","p"],["w"]])
        groups=VerificationCoordinator().schedule([w,a])
        self.assertEqual([[c.id for _,c in group] for _,group in groups],[["w","a"]])

    def test_failed_prerequisite_does_not_block_later_employment(self):
        claims=[claim("任职",ident="w",raw="张三在合成公司任工程师",company="合成公司"),claim()]
        calls=[]
        def collect(c,*a,**kw):calls.append(c.id);return [],False
        prep=SimpleNamespace(doc=SimpleNamespace(kind="md",source="synthetic",risks=[]),llm=object(),
                             profile=ResumeProfile(identity="researcher"),name="张三",resume_path="synthetic",claims=claims,skipped=[])
        with patch.object(pipeline,"collect_for_claim",collect),patch.object(pipeline,"employer_policy",return_value={}), \
             patch.object(pipeline,"generate_question",return_value="待补证"):
            report=pipeline.verify_prepared(prep,searcher=object(),fetcher=object())
        self.assertEqual(calls,["p","w"])
        self.assertEqual([vc.claim.id for vc in report.claims],["w","p"])
        self.assertEqual(report.verification_workflow["handoffs"],[])
        self.assertEqual(report.claims[0].status,"none")

    def test_work_query_consumes_confirmed_publication_anchor_without_copying_proof(self):
        from app.provenance import bind_page
        from app.schema import ElementProof
        p=claim();p.elements=["论文标题=Synthetic study"]
        body="张三发表Synthetic study"
        ev=bind_page(Evidence(url="https://publisher.example/study",title="论文原文",publisher="",source_tier="B",
                    snippet=body,identity_score=4,identity_signals=["学校一致","学院一致"],supports=p.elements),body)
        vc=VerifiedClaim(claim=p,status="part",proved=p.elements,evidence=[ev],
                         element_proofs=[ElementProof(element=p.elements[0],state="verified",
                                                      evidence_ids=[ev.evidence_id],accepted_tier="B")])
        coordinator=VerificationCoordinator();coordinator.record([vc])
        w=claim("任职",ident="w",raw="张三在合成公司任工程师",company="合成公司",role="工程师")
        w.elements=["单位=合成公司","岗位=工程师"]
        routed=coordinator.context_for(w)
        plan=Plan([Query('"合成公司" "张三"')],"test","test",budget={"searches":2,"reads":2,"seconds":20})
        agent_for(routed).refine_plan(routed,"张三",plan)
        self.assertIn("Synthetic study",plan.queries[0].text)
        self.assertEqual(plan.handoffs[0]["urls"],[ev.url]);self.assertEqual(plan.handoffs[0]["use"],"navigation_only")
        self.assertEqual(plan.budget["searches"],2)
        self.assertEqual(routed.elements,w.elements)
        self.assertNotIn("论文标题=Synthetic study",routed.elements)

    def test_unverified_other_report_and_search_summary_do_not_create_handoff(self):
        p=claim();p.elements=["论文标题=Synthetic study"]
        ev=Evidence(url="https://publisher.example/study",title="",publisher="",source_tier="C",snippet="张三 Synthetic study",identity_score=3,supports=p.elements,extraction_method="search_snippet")
        prior=VerifiedClaim(claim=p,status="part",proved=p.elements,evidence=[ev])
        a=VerificationCoordinator();a.record([prior]);b=VerificationCoordinator()
        for coordinator in (a,b):
            w=coordinator.context_for(claim("任职",ident="w"))
            plan=Plan([Query('"张三"')],"test","test",budget={"searches":2})
            agent_for(w).refine_plan(w,"张三",plan)
            self.assertEqual(plan.handoffs,[])

    def test_execution_routes_through_specialist_and_records_real_tools(self):
        c=claim("实习",ident="i",raw="张三在合成有限公司实习",company="合成有限公司")
        prep=SimpleNamespace(doc=SimpleNamespace(kind="md",source="synthetic",risks=[]),llm=object(),profile=ResumeProfile(identity="student"),name="张三",resume_path="synthetic",claims=[c],skipped=[])
        search=SimpleNamespace(search=lambda *a,**k:[])
        with patch.object(pipeline,"employer_policy",return_value={}):
            report=pipeline.verify_prepared(prep,searcher=search,fetcher=object())
        task=report.verification_workflow["tasks"][0]
        self.assertEqual(task["agent"]["id"],"internship")
        self.assertEqual(task["sourceAccess"]["events"][0]["sourceId"],"qcc_contacts")
        self.assertEqual(report.claims[0].evidence,[]);self.assertEqual(report.claims[0].proved,[])

    def test_only_selected_claims_are_scheduled_and_traced(self):
        w,p=claim("任职",ident="w"),claim()
        prep=SimpleNamespace(doc=SimpleNamespace(kind="md",source="synthetic",risks=[]),llm=object(),profile=ResumeProfile(),name="张三",resume_path="synthetic",claims=[w,p],skipped=[])
        with patch.object(pipeline,"employer_policy",return_value={}),patch.object(pipeline,"collect_for_claim",return_value=([],False)),patch.object(pipeline,"generate_question",return_value="待补证"):
            report=pipeline.verify_prepared(prep,claim_ids=["w"],searcher=object(),fetcher=object())
        self.assertEqual([t["claimId"] for t in report.verification_workflow["tasks"]],["w"])
        self.assertEqual(report.verification_workflow["batches"][0]["claimIds"],["w"])

    def test_pipeline_handoff_changes_next_search_but_not_its_proof(self):
        title="合成数据系统基础"
        book=claim("著作",ident="b",raw=f"张三主编《{title}》",title=title)
        book.elements=["书名="+title]
        work=claim("任职",ident="w",raw="张三在合成公司任工程师",company="合成公司",role="工程师")
        work.elements=["单位=合成公司","岗位=工程师"]
        searched=[]
        search=SimpleNamespace(search=lambda text,site=None: searched.append(text) or [])
        def collect(c,name,searcher,llm,**kw):
            if c.id=="b":
                from app.provenance import bind_page
                body=f"张三主编《{title}》"
                return [bind_page(Evidence(url="https://publisher.example/book",title="原始书目",publisher="独立出版社",
                                 source_tier="B",snippet=body,identity_signals=["具体经历一致"],supports=c.elements),body)],False
            self.assertIn(title,kw["queries"][0].text)
            searcher.search(kw["queries"][0].text)
            return [],False
        prep=SimpleNamespace(doc=SimpleNamespace(kind="md",source="synthetic",risks=[]),llm=object(),
                             profile=ResumeProfile(identity="researcher"),name="张三",resume_path="synthetic",claims=[work,book],skipped=[])
        with patch.object(pipeline,"employer_policy",return_value={}),patch.object(pipeline,"collect_for_claim",collect),patch.object(pipeline,"generate_question",return_value="待补证"):
            report=pipeline.verify_prepared(prep,searcher=search,fetcher=object())
        self.assertTrue(searched);self.assertIn(title,searched[0])
        self.assertEqual(report.verification_workflow["handoffs"][0]["fromClaim"],"b")
        self.assertEqual(report.verification_workflow["handoffs"][0]["toClaim"],"w")
        self.assertEqual(report.claims[0].status,"none")
        self.assertEqual(report.claims[0].evidence,[]);self.assertEqual(report.claims[0].proved,[])
        self.assertEqual(report.claims[0].verification_agent["sourceAccess"]["events"][0]["sourceId"],"public_web")

    def test_school_catalog_lookups_alone_cannot_prove_enrollment(self):
        c=claim("学历",ident="edu",raw="张三华南理工大学本科",org="华南理工大学")
        c.elements=["学校=华南理工大学"]
        prep=SimpleNamespace(doc=SimpleNamespace(kind="md",source="synthetic",risks=[]),llm=object(),profile=ResumeProfile(identity="student"),name="张三",resume_path="synthetic",claims=[c],skipped=[])
        with patch.object(pipeline,"employer_policy",return_value={}),patch.object(pipeline,"collect_for_claim",return_value=([],False)),patch.object(pipeline,"generate_question",return_value="待补证"):
            report=pipeline.verify_prepared(prep,searcher=object(),fetcher=object())
        events=report.verification_workflow["tasks"][0]["sourceAccess"]["events"]
        self.assertEqual(events[0]["sourceId"],"school_catalog")
        self.assertEqual(events[0]["state"],"returned")
        self.assertEqual(report.claims[0].status,"none")
        self.assertEqual(report.claims[0].proved,[])

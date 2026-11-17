"""Offline protocol and isolation tests; all fixtures and providers synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from app.schema import Claim,VerifiedClaim,ElementProof
from app.search import SearchHit,SearchUnavailable
from app.dual_search_experiment import (Deadline,ExperimentTimeout,DualSearchExperiment,
    DeadlineSearcher,DeadlineLLM,public_host,grounded,summarize_claims)


def run(category='奖学金',**entities):
    c=Claim(id='synthetic',category=category,raw_text='林示例2025年在清华大学获得示例奖学金',
        raw_locator='合成测试',date_label='2025',elements=['奖项=示例奖学金'],
        entities={'candidate_name':'林示例','school':'清华大学','award':'示例奖学金',**entities})
    return SimpleNamespace(claim=c,add=Mock(),snapshot=lambda:{'budget':{},'decisionCounts':{}})


class DualExperimentTests(unittest.TestCase):
    def setup_search(self,record=None,primary=None,planner=None,discovery=None,deadline=None):
        record=record or run()
        primary=primary or SimpleNamespace(search=Mock(return_value=[]),confirm_empty=Mock(return_value=[]))
        planner=planner or SimpleNamespace(complete_json=Mock(return_value={'queries':[], 'discoveryQuery':'林示例 2025 示例奖学金'}))
        discovery=discovery or SimpleNamespace(search=Mock(return_value=[]))
        search=DualSearchExperiment(primary,planner,discovery,deadline or Deadline(60),
            domains={'tsinghua.edu.cn','official.example.net'},run_getter=lambda:record)
        return search,record,primary,planner,discovery

    def test_baseline_query_is_preserved_and_native_discovery_follows_empty(self):
        native=SearchHit('https://official.example.net/list',title='2025示例奖学金名单',snippet='林示例')
        discovery=SimpleNamespace(search=Mock(return_value=[native]))
        s,r,p,m,d=self.setup_search(discovery=discovery)
        hits=s.search('林示例 示例奖学金')
        self.assertIn(native,hits);p.search.assert_any_call('林示例 示例奖学金',site=None)
        p.search.assert_any_call('林示例 示例奖学金',site='official.example.net')
        self.assertEqual(m.complete_json.call_count,2);self.assertEqual(d.search.call_count,1)

    def test_relevant_primary_hit_does_not_trigger_native_discovery(self):
        hit=SearchHit('https://www.tsinghua.edu.cn/list',title='示例奖学金',snippet='林示例')
        p=SimpleNamespace(search=Mock(return_value=[hit]),confirm_empty=Mock(return_value=[]))
        s,r,p,m,d=self.setup_search(primary=p);self.assertEqual(s.search('林示例 示例奖学金'),[hit]);d.search.assert_not_called()

    def test_one_plan_and_discovery_per_claim_prevents_feedback_loop(self):
        s,r,p,m,d=self.setup_search()
        for text in ('林示例 示例奖学金','林示例 2025 公示','林示例 获奖'):s.search(text)
        self.assertEqual(m.complete_json.call_count,2);self.assertEqual(d.search.call_count,1)

    def test_actual_glm_feedback_is_received_before_ds_native_search(self):
        hit=SearchHit('https://official.example.net/background',title='无关背景说明',snippet='没有本人或项目事实')
        p=SimpleNamespace(search=Mock(return_value=[hit]),confirm_empty=Mock(return_value=[]))
        s,r,p,m,d=self.setup_search(primary=p);s.search('林示例 示例奖学金')
        payload=m.complete_json.call_args.args[1]
        self.assertIn('glmResults',payload);self.assertIn('无关背景说明',payload)
        self.assertEqual(d.search.call_count,1)

    def test_grounded_plan_on_known_domain_runs_before_original_query(self):
        m=SimpleNamespace(complete_json=Mock(return_value={'queries':[{'query':'林示例 2025 名单','domain':'tsinghua.edu.cn'}]}))
        s,r,p,m,d=self.setup_search(planner=m);s.search('林示例 示例奖学金')
        self.assertEqual(p.search.call_args_list[0].args[0],'林示例 2025 名单')
        self.assertEqual(p.search.call_args_list[0].kwargs['site'],'tsinghua.edu.cn')

    def test_invented_domain_or_year_is_not_used_as_a_planner_probe(self):
        m=SimpleNamespace(complete_json=Mock(return_value={'queries':[
            {'query':'林示例 2024 名单','domain':'tsinghua.edu.cn'},
            {'query':'林示例 名单','domain':'invented.example.net'}]}))
        s,r,p,m,d=self.setup_search(planner=m);s.search('林示例 示例奖学金')
        self.assertFalse(any(c.kwargs.get('site')=='invented.example.net' or '2024' in c.args[0] for c in p.search.call_args_list))

    def test_existing_publisher_not_in_catalog_keeps_baseline_route(self):
        s,r,p,m,d=self.setup_search();s.search('林示例 示例奖学金',site='publisher.example.net')
        p.search.assert_any_call('林示例 示例奖学金',site='publisher.example.net')

    def test_employer_is_excluded_from_model_probes_and_discovered_hits(self):
        r=run(excluded_employer_sources={'domains':['tsinghua.edu.cn'],'organizations':['清华大学']})
        d=SimpleNamespace(search=Mock(return_value=[SearchHit('https://www.tsinghua.edu.cn/notice',title='名单',snippet='林示例')]))
        s,r,p,m,d=self.setup_search(record=r,discovery=d)
        self.assertEqual(s.search('林示例 示例奖学金'),[])
        self.assertFalse(any(c.kwargs.get('site')=='tsinghua.edu.cn' for c in p.search.call_args_list))

    def test_native_url_is_real_returned_lead_and_does_not_become_proof(self):
        from app import judge
        r=run();original=r.claim.model_dump()
        d=SimpleNamespace(search=Mock(return_value=[SearchHit('https://novel.example.net/notice',title='2025示例奖学金',snippet='林示例获奖')]))
        s,r,p,m,d=self.setup_search(record=r,discovery=d);hits=s.search('林示例 示例奖学金')
        self.assertEqual(len(hits),1);self.assertEqual(r.claim.model_dump(),original)
        self.assertEqual(judge.assess(r.claim,[]).proved,[])
        p.search.assert_any_call('林示例 示例奖学金',site='novel.example.net')

    def test_private_or_credentialed_discovery_urls_are_rejected(self):
        d=SimpleNamespace(search=Mock(return_value=[SearchHit('http://127.0.0.1/admin'),SearchHit('https://user:secret@official.example.net/list')]))
        s,r,p,m,d=self.setup_search(discovery=d);self.assertEqual(s.search('林示例 示例奖学金'),[])

    def test_source_failures_preserve_original_hits_and_audit_reason(self):
        m=SimpleNamespace(complete_json=Mock(side_effect=RuntimeError('synthetic')))
        d=SimpleNamespace(search=Mock(side_effect=SearchUnavailable('synthetic')))
        s,r,p,m,d=self.setup_search(planner=m,discovery=d)
        self.assertEqual(s.search('林示例 示例奖学金'),[])
        actions=[c.args[0] for c in r.add.call_args_list]
        self.assertTrue(any(e['action']=='ds_plan' and e['state']=='failed' for e in actions))
        self.assertTrue(any(e['action']=='ds_discovery' and e['state']=='failed' for e in actions))

    def test_no_specialist_context_keeps_original_background_search(self):
        s,r,p,m,d=self.setup_search();s.run_getter=lambda:None
        s.search('合成学校简介',site='tsinghua.edu.cn');m.complete_json.assert_not_called();d.search.assert_not_called()
        p.search.assert_called_once()

    def test_deadline_prevents_all_new_provider_calls(self):
        s,r,p,m,d=self.setup_search(deadline=Deadline(end=0,clock=lambda:1))
        with self.assertRaises(ExperimentTimeout):s.search('林示例 示例奖学金')
        p.search.assert_not_called();m.complete_json.assert_not_called();d.search.assert_not_called()

    def test_deadline_wrappers_cover_model_and_confirm_requests(self):
        inner=Mock();deadline=Deadline(end=0,clock=lambda:1)
        with self.assertRaises(ExperimentTimeout):DeadlineLLM(inner,deadline).complete_json('system','test')
        with self.assertRaises(ExperimentTimeout):DeadlineSearcher(inner,deadline).confirm_empty('test')
        self.assertEqual(inner.mock_calls,[])

    def test_time_remaining_shared_across_calls(self):
        now=[10];d=Deadline(900,clock=lambda:now[0]);now[0]=909;self.assertEqual(d.remaining(),1)
        now[0]=910
        with self.assertRaises(ExperimentTimeout):d.check()

    def test_model_suggestions_need_original_anchor_and_year(self):
        r=run()
        self.assertTrue(grounded('林示例 2025 示例奖学金',r.claim,'林示例'))
        self.assertFalse(grounded('林示例 2024 示例奖学金',r.claim,'林示例'))
        self.assertFalse(grounded('无关人员 获奖',r.claim,'林示例'))

    def test_public_url_validation(self):
        self.assertEqual(public_host('https://www.tsinghua.edu.cn/path'),'www.tsinghua.edu.cn')
        for url in ('http://localhost/a','http://10.0.0.1/a','https://a.internal/a','https://a:b@public.edu/a','file:///etc/passwd'):
            self.assertEqual(public_host(url),'')

    def test_production_factory_does_not_select_experiment(self):
        from app.search import build_searcher,ZhipuSearcher
        with patch.dict(os.environ,{'UNDERBOX_ICT_GLM_ONLY':'1','SEARCH_API_KEY':'synthetic-key'}):
            self.assertIsInstance(build_searcher(),ZhipuSearcher)

    def test_metrics_count_verified_elements_and_keep_manual_claims_separate(self):
        c=run().claim;vc=VerifiedClaim(claim=c,status='part',element_proofs=[ElementProof(element=c.elements[0],state='partial')])
        manual=VerifiedClaim(claim=c.model_copy(update={'id':'manual','category':'实习'}),status='ask')
        m=summarize_claims([vc,manual]);self.assertEqual(m['verifiedElements'],0);self.assertEqual(m['partialElements'],1)
        self.assertEqual(m['totalElements'],1);self.assertEqual(m['manualClaims'],1)

    def test_runner_rejects_production_output_before_reading_credentials(self):
        path=Path(__file__).resolve().parents[2]/'scripts/compare_search.py'
        spec=importlib.util.spec_from_file_location('synthetic_compare_runner',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.subprocess,'check_output') as process:
            with self.assertRaises(ValueError):module.live_environment('/var/lib/underbox-ict-full/unsafe')
        process.assert_not_called()

    def test_private_checkpoint_permissions_and_atomic_json(self):
        path=Path(__file__).resolve().parents[2]/'scripts/compare_search.py'
        spec=importlib.util.spec_from_file_location('synthetic_compare_runner',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'checkpoint.json';module.private_write(p,{'synthetic':True})
            self.assertEqual(json.loads(p.read_text()),{'synthetic':True});self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_section_split_retains_late_patents_and_work_without_twenty_item_ceiling(self):
        path=Path(__file__).resolve().parents[2]/'scripts/compare_search.py'
        spec=importlib.util.spec_from_file_location('synthetic_compare_runner',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        chunks=module.split_sections('候选人：林示例\n教育背景\n清华大学2025年就读本科，合成资料不代表真实用户。\n专利：\n'+('合成测试专利登记号及题名。\n'*25)+'实习经历\n2025合成公司实习，负责合成测试相关工作，内容仅为测试资料。')
        self.assertEqual(len(chunks),3);self.assertIn('合成测试专利',chunks[1]);self.assertIn('合成公司',chunks[2])

    def test_unheaded_input_is_not_lost_during_common_split(self):
        path=Path(__file__).resolve().parents[2]/'scripts/compare_search.py'
        spec=importlib.util.spec_from_file_location('synthetic_compare_runner',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.split_sections('合成的一段经历，没有章节标题。'),['合成的一段经历，没有章节标题。'])

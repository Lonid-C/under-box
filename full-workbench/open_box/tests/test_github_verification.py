"""合成公开 API 响应验证按需触发、证据用途、范围与失败降级；不访问真实账号。"""
import json,unittest
from unittest.mock import patch
import httpx
from app.schema import Claim,Evidence
from app.strategy import github_trigger,build_plan
from app.github_audit import GitHubClient,audit_claim,bind_input,parse_link,supplement_claims
from app.claim_scope import enrich_claim
from app.collect import collect_for_claim,Budget
from app import judge,split,pipeline

REPO='team/demo';PR='https://github.com/team/demo/pull/7'

def claim(raw=None,accounts=None,elements=None,category='开源项目'):
    return enrich_claim(Claim(id='c1',category=category,raw_text=raw or '为 demo 项目贡献文档 '+PR,raw_locator='第1页',date_label='',elements=elements or ['项目=demo'],entities={'title':'demo','github_accounts':['alice'] if accounts is None else accounts}))

class FakeAPI:
    def __init__(self,files=None,actor='alice',merged=True,private=False,status=None,account_type='User'):
        self.requests=[];self.files=files or [{'filename':'docs/guide.md','patch':'@@ -1 +1 @@\n-old\n+new','additions':1,'deletions':1,'status':'modified'}]
        self.actor=actor;self.merged=merged;self.private=private;self.status=status;self.account_type=account_type
    def handle(self,r):
        path=r.url.path;self.requests.append(str(r.url))
        if self.status: return httpx.Response(self.status,json={'message':'synthetic'})
        if path=='/users/alice':data={'login':'alice','type':self.account_type}
        elif path=='/repos/'+REPO:data={'full_name':REPO,'name':'demo','private':self.private,'owner':{'login':'team','type':'Organization'},'fork':False,'stargazers_count':800,'language':'Python'}
        elif path=='/repos/'+REPO+'/pulls/7':data={'number':7,'title':'Update guide','user':{'login':self.actor},'merged':self.merged,'state':'closed' if self.merged else 'open','changed_files':len(self.files)}
        elif path.endswith('/pulls/7/files'): data=self.files
        elif path.endswith('/pulls'):data=[{'number':7,'user':{'login':self.actor}}]
        elif path.endswith('/commits'):data=[]
        elif '/commits/' in path:data={'sha':'a'*40,'author':{'login':self.actor},'commit':{'message':'Change parser','verification':{'verified':True,'reason':'valid'}},'files':self.files}
        else: raise AssertionError('调查范围外请求：'+str(r.url))
        return httpx.Response(200,json=data)
    def client(self,max_requests=20): return GitHubClient(transport=httpx.MockTransport(self.handle),max_requests=max_requests)

class GateTests(unittest.TestCase):
    def test_actual_project_and_associated_link_trigger(self):
        c=claim();self.assertEqual(len(github_trigger(c)),1)
        self.assertEqual(build_plan(c,'张三').profile_id,'github_contribution')
        self.assertEqual(build_plan(c,'张三').budget['searches'],0)
    def test_no_link_profile_only_and_ordinary_claim_never_call_github(self):
        raws=['开发 demo 项目','开发 demo 项目 https://github.com/alice','熟悉 GitHub https://github.com/team/demo']
        for raw in raws:
            c=claim(raw=raw)
            self.assertEqual(github_trigger(c),[])
            with patch('app.collect.github_hits') as old,patch('app.github_audit.GitHubClient') as api:
                result,_=collect_for_claim(c,'张三',object(),object(),queries=[],budget=Budget(max_searches=0,max_page_reads=0))
                self.assertEqual(result,[]);old.assert_not_called();api.assert_not_called()
        self.assertEqual(github_trigger(claim(category='学历')),[])
    def test_raw_input_binding_does_not_accept_hallucinated_urls_or_infer_owner(self):
        raw='参与 demo 项目';text=raw+'\n\nGitHub: https://github.com/alice'
        c=Claim(id='c1',category='项目',raw_text=raw,raw_locator='',date_label='',entities={'github':'bob','repo':REPO,'provided_urls':[PR]})
        c=bind_input([c],text)[0]
        self.assertEqual(c.entities['github_accounts'],['alice']);self.assertNotIn('github',c.entities)
        self.assertEqual(github_trigger(c),[])
        c.raw_text+=' '+PR
        self.assertEqual(github_trigger(c),[], '模型改写 raw_text 不能绕过原始输入校验')
    def test_adjacent_associated_link_is_bound_but_not_shared_across_projects(self):
        a=claim(raw='参与 demo 项目');b=claim(raw='参与 other 项目');b.id='c2'
        text='GitHub: https://github.com/alice\n\n'+a.raw_text+'\n'+PR+'\n\n'+b.raw_text
        out=bind_input([a,b],text)
        self.assertEqual(len(github_trigger(out[0])),1);self.assertEqual(github_trigger(out[1]),[])
    def test_account_only_never_enumerates_repositories(self):
        c=claim(raw='开发 demo https://github.com/alice')
        self.assertEqual(github_trigger(c),[])
    def test_spoof_host_credentials_and_unsupported_links_are_rejected(self):
        for u in ['https://github.com.evil.test/a/r','http://alice@github.com/a/r','https://github.com:bad/a/r','https://github.com/a/r/issues/1','https://github.com/a/../b']:
            self.assertIsNone(parse_link(u))
    def test_supplement_is_explicit_and_only_applies_to_chosen_project(self):
        a=claim(raw='参与 demo，源码有 GitHub 仓库');b=claim(raw='参与 other');b.id='c2'
        changed=supplement_claims([a,b],{'c1':{'accounts':['https://github.com/alice'],'urls':[PR]}})
        self.assertTrue(github_trigger(changed[0]));self.assertFalse(github_trigger(changed[1]))
        with self.assertRaises(ValueError):supplement_claims([a],{'c1':{'urls':['https://evil.test/x']}})
        with self.assertRaises(ValueError):supplement_claims([a],{'unknown':{'urls':[PR]}})
    def test_split_enriches_literal_modifiers_and_validates_link_source(self):
        raw='主导自研 demo，性能提升30% '+PR
        c=claim(raw=raw)
        class LLM:
            def complete_json(self,*a,**k):return {'claims':[c.model_dump()]}
        out=split.split_claims('GitHub: https://github.com/alice\n\n'+raw,LLM())[0]
        self.assertTrue(github_trigger(out));self.assertIn('个人角色=主导',out.elements);self.assertIn('修饰语=自研',out.elements);self.assertIn('量化指标=性能提升30%',out.elements)

class ContributionTests(unittest.TestCase):
    def audit(self,c,api,llm=None,budget=None):
        client=api.client()
        try:
            evidence,exhausted=audit_claim(c,github_trigger(c),llm,client=client,budget=budget)
            return judge.assess(c,evidence,search_exhausted=exhausted)
        finally:client.close()
    def test_real_participation_is_supported_without_star_or_lines_as_role(self):
        c=claim(raw='参与 demo 项目 '+PR)
        api=FakeAPI();v=self.audit(c,api)
        self.assertEqual(v.status,'ok');self.assertIn('个人角色=参与',v.proved);self.assertIn('本人参与=该项目',v.proved)
        self.assertEqual(v.best_tier,'B');self.assertTrue(v.github_audit['identityConfirmed'])
        self.assertEqual(v.github_audit['repositories'][0]['kind'],'外部/组织仓库')
        self.assertTrue(all('team/demo' in u or '/users/alice' in u for u in api.requests))
    def test_documents_do_not_prove_core_architecture_or_leadership(self):
        c=claim(raw='主导 demo，作为核心贡献者参与核心架构 '+PR)
        v=self.audit(c,FakeAPI());self.assertEqual(v.status,'part')
        for e in ['个人角色=主导','个人角色=核心贡献者','修饰语=核心架构']:self.assertIn(e,v.unproved)
        self.assertEqual(v.github_audit['records'][1]['files'][0]['type'],'文档')
        self.assertFalse(v.evidence[1].contradicts,'不把单个文档 PR 推断为整个项目履历冲突')
    def test_specific_pr_architecture_and_merge_conflicts_are_narrow(self):
        c=claim(raw='该 PR 修改核心架构并已合并 '+PR)
        v=self.audit(c,FakeAPI(merged=False));self.assertEqual(v.status,'ask')
        self.assertTrue(any('merged=false' in d for e in v.evidence for d in e.contradicts))
    def test_repo_owner_does_not_prove_independent_or_self_developed(self):
        c=claim(raw='独立开发、自研 demo https://github.com/team/demo')
        v=self.audit(c,FakeAPI());self.assertEqual(v.status,'part');self.assertIn('个人角色=独立开发',v.unproved);self.assertIn('修饰语=自研',v.unproved)
    def test_unknown_account_stops_all_api_and_diff(self):
        c=claim(accounts=[]);api=FakeAPI();v=self.audit(c,api)
        self.assertEqual(v.status,'who');self.assertEqual(api.requests,[]);self.assertEqual(v.evidence,[])
    def test_another_pr_author_is_excluded_and_no_diff_read(self):
        api=FakeAPI(actor='bob');v=self.audit(claim(),api)
        self.assertEqual(v.status,'who');self.assertFalse(any(u.endswith('/files') for u in api.requests));self.assertFalse(any(e.github_record.get('actor')=='bob' for e in v.evidence))
    def test_private_and_rate_limit_are_neutral(self):
        for api in [FakeAPI(private=True),FakeAPI(status=404),FakeAPI(status=403),FakeAPI(status=429)]:
            v=self.audit(claim(),api);self.assertEqual(v.status,'none');self.assertEqual(v.evidence,[]);self.assertTrue(v.github_audit['warnings'])
    def test_metrics_are_not_invented_from_code_or_diff_model(self):
        class MaliciousModel:
            def complete_json(self,*a,**k):return {'supports':['个人角色=主导','量化指标=性能提升30%'],'observations':[{'path':'unknown.py','quote':'not present','description':'评价'}]}
        c=claim(raw='主导 demo，性能提升30% '+PR);v=self.audit(c,FakeAPI(),MaliciousModel())
        self.assertIn('量化指标=性能提升30%',v.unproved);self.assertIn('个人角色=主导',v.unproved);self.assertIsNone(v.github_audit['records'][1]['analysis'])
    def test_linked_commit_checks_api_account_not_author_display_name(self):
        c=claim(raw='参与 demo https://github.com/team/demo/commit/'+'a'*40)
        v=self.audit(c,FakeAPI());self.assertEqual(v.status,'ok');self.assertEqual(v.github_audit['records'][1]['kind'],'commit')
    def test_no_diff_does_not_invent_technical_depth(self):
        api=FakeAPI(files=[{'filename':'src/core.py','additions':40,'deletions':0}]);v=self.audit(claim(raw='核心贡献者参与 demo '+PR),api)
        self.assertEqual(v.status,'part');self.assertIn('个人角色=核心贡献者',v.unproved);self.assertTrue(v.github_audit['records'][1]['files'][0]['patchTruncated'])
    def test_request_budget_truncates_and_does_not_expand(self):
        client=FakeAPI().client(max_requests=2)
        try:
            e,ex=audit_claim(claim(),github_trigger(claim()),client=client)
            self.assertTrue(ex);self.assertEqual(client.calls,2);self.assertTrue(e.github_audit['truncated'])
        finally:client.close()
    def test_files_pagination_marks_partial_range(self):
        calls=[]
        def route(r):
            calls.append(r.url.params.get('page'));return httpx.Response(200,json=[{'filename':'x.py'}],headers={'Link':'<https://api.github.com/repos/team/demo/pulls/7/files?page=9>; rel="next"'})
        c=GitHubClient(transport=httpx.MockTransport(route))
        try:
            files,truncated=c.pages('/repos/team/demo/pulls/7/files',max_pages=2)
            self.assertEqual(len(files),2);self.assertTrue(truncated);self.assertEqual(calls,['1','2'])
        finally:c.close()
    def test_generic_evidence_does_not_use_other_persons_role(self):
        c=claim(raw='主导 demo 项目',accounts=[])
        c.entities['candidate_name']='张三'
        e=Evidence(url='https://company.example/a',title='团队',publisher='公司',source_tier='B',snippet='张三参与demo；李四主导demo。',identity_signals=['任职单位一致'],supports=list(c.elements))
        # 完整摘录出现两个人时不能单凭同段关键字支持强角色。
        v=judge.assess(c,[e]);self.assertIn('个人角色=主导',v.unproved)

class ScopeBoundaryTests(unittest.TestCase):
    def test_english_core_modifier_is_not_plain_contributor(self):
        c=claim(raw='Core contributor to demo '+PR)
        api=FakeAPI();client=api.client()
        try:e,x=audit_claim(c,github_trigger(c),client=client)
        finally:client.close()
        v=judge.assess(c,e)
        self.assertEqual(v.status,'part');self.assertIn('个人角色=Core contributor',v.unproved)
    def test_negated_role_and_helper_do_not_prove_leadership(self):
        for text in ['张三不是主导demo的人。','张三协助同事主导demo。']:
            c=claim(raw='主导demo',accounts=[]);c.entities['candidate_name']='张三'
            e=Evidence(url='https://company.example/a',title='团队',publisher='公司',source_tier='B',snippet=text,identity_signals=['任职单位一致'],supports=c.elements)
            self.assertIn('个人角色=主导',judge.assess(c,[e]).unproved)
    def test_plain_language_person_role_can_be_supported(self):
        from app.provenance import bind_page
        c=claim(raw='主导 demo 项目',accounts=[]);c.entities['candidate_name']='张三'
        body='张三主导 demo 项目。'
        e=bind_page(Evidence(url='https://company.example/a',title='团队',publisher='公司',source_tier='B',snippet=body,identity_signals=['任职单位一致'],supports=c.elements),body)
        self.assertIn('个人角色=主导',judge.assess(c,[e]).proved)
    def test_incomplete_files_do_not_make_whole_pr_architecture_conflict(self):
        api=FakeAPI();base=api.handle
        def route(r):
            response=base(r)
            if r.url.path.endswith('/pulls/7'):
                obj=response.json();obj['changed_files']=200;return httpx.Response(200,json=obj)
            return response
        client=GitHubClient(transport=httpx.MockTransport(route));c=claim(raw='该 PR 修改核心架构 '+PR)
        try:e,x=audit_claim(c,github_trigger(c),client=client)
        finally:client.close()
        v=judge.assess(c,e)
        self.assertEqual(v.status,'part');self.assertTrue(v.github_audit['truncated']);self.assertFalse(any(e.contradicts for e in v.evidence))
    def test_input_supplement_does_not_mutate_cached_claim(self):
        c=claim(raw='开发 demo，源码有 GitHub 仓库');old=c.model_dump()
        result=supplement_claims([c],{'c1':{'urls':[PR]}})
        self.assertEqual(c.model_dump(),old);self.assertTrue(github_trigger(result[0]))
    def test_github_route_respects_explicit_read_budget(self):
        api=FakeAPI();c=claim();budget=Budget(max_searches=0,max_page_reads=0)
        with patch('app.github_audit.httpx.Client',return_value=httpx.Client(transport=httpx.MockTransport(api.handle))):
            e,x=collect_for_claim(c,'张三',object(),object(),budget=budget)
        self.assertEqual(api.requests,[]);self.assertTrue(x)

class CommitAttributionTests(unittest.TestCase):
    def test_unsigned_email_association_is_not_authenticated_contribution(self):
        api=FakeAPI();base=api.handle
        def route(r):
            result=base(r)
            if '/commits/' in r.url.path:
                obj=result.json();obj['commit']['verification']={'verified':False,'reason':'unsigned'}
                return httpx.Response(200,json=obj)
            return result
        c=claim(raw='参与 demo https://github.com/team/demo/commit/'+'a'*40)
        client=GitHubClient(transport=httpx.MockTransport(route))
        try:e,x=audit_claim(c,github_trigger(c),client=client)
        finally:client.close()
        v=judge.assess(c,e)
        self.assertEqual(v.status,'part');self.assertIn('本人参与=该项目',v.unproved);self.assertTrue(v.needs_human);self.assertTrue(v.github_audit['unverifiedCommit'])

class PipelineTests(unittest.TestCase):
    def test_pipeline_routes_only_linked_project_and_keeps_role_unknown(self):
        import os
        from types import SimpleNamespace
        from app.strategy import ResumeProfile
        from app.pipeline import PreparedResume
        c=claim(raw='核心贡献者参与 demo '+PR)
        class Model:
            def complete_json(self,*args,**kwargs):return {}
        class NoSources:
            def search(self,*args,**kwargs):raise AssertionError('GitHub 条目不进行宽泛网页搜索')
            def get(self,*args,**kwargs):raise AssertionError('GitHub 条目不抓其他网页')
        prepared=PreparedResume(resume_path='synthetic',doc=SimpleNamespace(kind='text',source='',risks=[]),llm=Model(),profile=ResumeProfile(identity='creator'),claims=[c],name='张三')
        api=FakeAPI();client=api.client()
        with patch.dict(os.environ,{'CLAIM_WORKERS':'1'}), patch('app.github_audit.GitHubClient',return_value=client):
            report=pipeline.verify_prepared(prepared,searcher=NoSources(),fetcher=NoSources())
        v=report.claims[0]
        self.assertEqual(v.status,'part');self.assertIsNotNone(v.github_audit);self.assertIsNotNone(v.claim_verification);self.assertIsNone(v.question)
        self.assertIn('个人角色=核心贡献者',v.unproved)
    def test_diff_analysis_does_not_introduce_hiring_advice_or_scores(self):
        from app.github_audit import diff_analysis
        class Model:
            def complete_json(self,*args,**kwargs):return {'observations':[{'path':'src/core.py','quote':'+return 1','description':'建议录用，评分100'}]}
        self.assertIsNone(diff_analysis(Model(),[{'path':'src/core.py','patch':'+return 1'}]))

class ExactRecordBoundaryTests(unittest.TestCase):
    def test_repository_existence_does_not_prove_missing_linked_pr(self):
        api=FakeAPI();base=api.handle
        def route(r):
            if r.url.path.endswith('/pulls/7'):return httpx.Response(404,json={'message':'Not Found'})
            return base(r)
        client=GitHubClient(transport=httpx.MockTransport(route));c=claim()
        try:e,x=audit_claim(c,github_trigger(c),client=client)
        finally:client.close()
        v=judge.assess(c,e)
        self.assertIn('关联项目链接='+PR,v.unproved);self.assertIn('本人参与=该项目',v.unproved)
        self.assertIn('项目=demo',v.proved);self.assertEqual(v.status,'part')
    def test_files_unavailable_keeps_authenticated_pr_metadata_as_c_level(self):
        api=FakeAPI();base=api.handle
        def route(r):
            if r.url.path.endswith('/files'):return httpx.Response(429,json={'message':'rate limited'})
            return base(r)
        client=GitHubClient(transport=httpx.MockTransport(route));c=claim(raw='参与 demo '+PR)
        try:e,x=audit_claim(c,github_trigger(c),client=client)
        finally:client.close()
        v=judge.assess(c,e)
        self.assertIn('本人参与=该项目',v.proved);self.assertEqual(v.status,'part');self.assertEqual(v.best_tier,'C')
        self.assertTrue(v.github_audit['warnings']);self.assertFalse(v.github_audit['records'][1]['filesComplete'])

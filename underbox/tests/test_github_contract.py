import sys,json,time,threading,http.client,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import serve
from app.schema import Claim

class GithubContractTests(unittest.TestCase):
    def test_brief_gates_github_entry_from_input_not_category(self):
        from app.github_audit import bind_input, supplement_claims
        cases=[('开发普通项目',False),('组织钢琴公开课',False),
               ('开发项目，源码在 GitHub 仓库',True),('开发项目 https://github.com/team/demo',True)]
        for raw,wanted in cases:
            c=Claim(id='c1',category='项目',raw_text=raw,raw_locator='',date_label='')
            c=bind_input([c],raw)[0]
            self.assertEqual(serve.claim_brief(c)['githubMaterialEligible'],wanted)
            if not wanted:
                with self.assertRaises(ValueError):supplement_claims([c],{'c1':{'urls':['https://github.com/team/demo']}})
    def test_materials_are_account_bound_and_validated_before_job(self):
        c=Claim(id='c1',category='项目',raw_text='开发 demo，源码有 GitHub 仓库',raw_locator='',date_label='')
        token='synthetic-test-token';called=[]
        class H(serve.Handler):
            def _authed(self):return True
            def _sse(self,target,args,kind=''):
                called.append((target,args,kind));self._json({'ok':True})
        with serve._PREPARED_LOCK:serve.PREPARED[token]={'owner':'a','expires_at':time.time()+3600,'prepared':SimpleNamespace(claims=[c])}
        server=ThreadingHTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
        def post(uid,materials):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=4)
            conn.request('POST','/api/verify/stream',json.dumps({'token':token,'claimIds':['c1'],'githubMaterials':materials}),{'X-Underbox-User':uid,'Content-Type':'application/json'})
            r=conn.getresponse();r.read();status=r.status;conn.close();return status
        try:
            self.assertEqual(post('b',{}),404);self.assertEqual(called,[])
            self.assertEqual(post('a',{'c1':{'urls':['http://localhost/private']}}),400);self.assertEqual(called,[])
            self.assertEqual(post('a',{'c1':{'accounts':['https://github.com/alice'],'urls':['https://github.com/team/demo/pull/7']}}),200)
            self.assertEqual(len(called),1);self.assertEqual(called[0][0].keywords['materials']['c1']['accounts'],['https://github.com/alice'])
            self.assertNotIn('github_bound_urls',c.entities)
        finally:
            server.shutdown();server.server_close()
            with serve._PREPARED_LOCK:serve.PREPARED.pop(token,None)
    def test_report_contract_keeps_scope_and_unproved_elements_in_export(self):
        from app.schema import VerifiedClaim,Report
        c=Claim(id='c1',category='开源项目',raw_text='核心贡献者',raw_locator='',date_label='',elements=['个人角色=核心贡献者'])
        audit={'triggered':True,'identityBasis':'候选人主动提供账号','scope':['https://github.com/team/demo/pull/7'],'records':[{'url':'https://github.com/team/demo/pull/7','files':[{'path':'docs/README.md','type':'文档','additions':1,'deletions':1,'patch':'-old\n+new'}]}],'warnings':[],'limitations':['不按数量评价能力']}
        vc=VerifiedClaim(claim=c,status='part',unproved=c.elements,github_audit=audit,claim_verification={'checks':[{'element':c.elements[0],'state':'尚无充分证据','urls':[]}]})
        report=Report(id='t',candidate_name='合成测试',position='',report_no='SYNTHETIC',generated_at='',claims=[vc])
        data=serve.to_presentation(report);md=serve.report_to_markdown(data)
        self.assertTrue(data['claims'][0]['githubAudit']['triggered']);self.assertIn('docs/README.md',md);self.assertIn('个人角色=核心贡献者',md);self.assertIn('尚无充分证据',md)
        self.assertEqual(Report.model_validate_json(report.model_dump_json()).claims[0].github_audit,audit)
        legacy=report.model_dump();legacy['claims'][0].pop('github_audit');legacy['claims'][0].pop('claim_verification')
        self.assertIsNone(Report.model_validate(legacy).claims[0].github_audit)

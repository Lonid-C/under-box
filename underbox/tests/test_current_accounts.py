"""SQLite/shared-worker account and history regression tests for the current architecture."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import store
import serve
from accounts import Accounts, account_handler

class CurrentAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'users.db'
        self.accounts=Accounts(self.db)
        with store.connect(self.db) as conn:
            for uid in ('test-a','test-b'): store.create_user(conn,uid,uid,'test-pass')
            for i in range(12): store.save_report(conn,'test-a',{'reportNo':str(i),'candidate':'合成甲','claims':[]})
            self.b=store.save_report(conn,'test-b',{'reportNo':'b','candidate':'合成乙','claims':[]});conn.commit()
        self.env=patch.dict(os.environ,{'UNDERBOX_DB':str(self.db),'UNDERBOX_PASSWORD':''});self.env.start()
        self.worker=ThreadingHTTPServer(('127.0.0.1',0),serve.Handler)
        self.gateway=ThreadingHTTPServer(('127.0.0.1',0),account_handler(serve.Handler,self.accounts,self.worker.server_port))
        for server in (self.worker,self.gateway): threading.Thread(target=server.serve_forever,daemon=True).start()
        self.cookie='ub_account='+self.accounts.login('test-a')
    def tearDown(self):
        for server in (self.gateway,self.worker): server.shutdown();server.server_close()
        self.env.stop();self.tmp.cleanup()
        with serve._STATE_LOCK:
            serve._STATES.pop('test-a',None);serve._STATES.pop('test-b',None)
    def raw_request(self,method,path,cookie=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=4)
        conn.request(method,path,headers={'Cookie':cookie if cookie is not None else self.cookie,**(headers or {})})
        res=conn.getresponse();raw=res.read();status=res.status;hs=dict(res.getheaders());conn.close();return status,hs,raw
    def request(self,method,path,cookie=None,headers=None):
        status,_,raw=self.raw_request(method,path,cookie,headers)
        return status,json.loads(raw)
    def test_history_trim_and_cross_account_denial_even_with_forged_header(self):
        status,data=self.request('GET','/api/history')
        self.assertEqual(status,200);self.assertEqual(len(data['items']),10)
        self.assertEqual([r['reportNo'] for r in data['items']],[str(i) for i in range(11,1,-1)])
        self.assertEqual(self.request('GET',f'/api/history/{self.b}')[0],404)
        self.assertEqual(self.request('GET',f'/api/history/{self.b}',headers={'X-Underbox-User':'test-b'})[0],409)
    def test_query_reset_retains_session_and_history_and_other_account(self):
        serve.user_state('test-a').update(presentation={'fake':True},oby=object())
        serve.user_state('test-b')['presentation']={'other':True}
        self.assertEqual(self.request('POST','/api/query/reset')[0],200)
        self.assertEqual(self.request('GET','/api/session')[1]['authed'],True)
        self.assertIsNone(serve.user_state('test-a')['presentation'])
        self.assertEqual(serve.user_state('test-b')['presentation'],{'other':True})
        self.assertEqual(len(self.request('GET','/api/history')[1]['items']),10)
    def test_history_replay_and_json_markdown_exports_are_account_scoped(self):
        items=self.request('GET','/api/history')[1]['items']
        latest=items[0]['id']
        report=self.request('GET',f'/api/history/{latest}')[1]['report']
        self.assertEqual(report['reportNo'],'11')
        status,headers,raw=self.raw_request('GET',f'/api/history/{latest}/export?format=json')
        self.assertEqual(status,200)
        self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(json.loads(raw),report)
        status,headers,raw=self.raw_request('GET',f'/api/history/{latest}/export?format=md')
        self.assertEqual(status,200)
        self.assertIn('attachment',headers['Content-Disposition'])
        self.assertIn('11',raw.decode('utf-8'))
        for suffix in ('','/export?format=json','/export?format=md'):
            self.assertEqual(self.raw_request('GET',f'/api/history/{self.b}{suffix}')[0],404)
        with store.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM report_history WHERE user_id='test-a' AND report_no='0'").fetchone())
    def test_current_report_and_export_do_not_cross_accounts(self):
        serve.user_state('test-a')['presentation']={'reportNo':'owned-a','candidateName':'合成甲','claims':[]}
        serve.user_state('test-b')['presentation']={'reportNo':'owned-b','candidateName':'合成乙','claims':[]}
        cookie_b='ub_account='+self.accounts.login('test-b')
        self.assertEqual(self.request('GET','/api/report')[1]['report']['reportNo'],'owned-a')
        self.assertEqual(self.request('GET','/api/report',cookie_b)[1]['report']['reportNo'],'owned-b')
        self.assertEqual(json.loads(self.raw_request('GET','/api/export?format=json')[2])['reportNo'],'owned-a')
        self.assertEqual(json.loads(self.raw_request('GET','/api/export?format=json',cookie_b)[2])['reportNo'],'owned-b')
        self.assertEqual(self.request('POST','/api/query/reset')[0],200)
        self.assertEqual(self.request('GET','/api/report')[0],404)
        status,_,raw=self.raw_request('GET','/api/export?format=json')
        self.assertEqual(status,409)  # Existing API contract: no current report to export.
        self.assertEqual(json.loads(raw)['error'],'还没有核验任何简历')
        self.assertEqual(self.request('GET','/api/report',cookie_b)[1]['report']['reportNo'],'owned-b')
    def test_logout_blocks_history_until_same_user_logs_in_again(self):
        self.assertEqual(self.request('POST','/api/logout')[0],200)
        self.assertEqual(self.request('GET','/api/history')[0],401)
        self.cookie='ub_account='+self.accounts.login('test-a')
        self.assertEqual(len(self.request('GET','/api/history')[1]['items']),10)

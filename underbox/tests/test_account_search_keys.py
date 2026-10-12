"""Account-key isolation through the real gateway/SSE and nested executor paths."""
import concurrent.futures
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
import store,serve
from accounts import Accounts,account_handler
from app.api_config import search_credentials
from app.search import build_searcher
from app.llm import build_llm
from app.usage import UsageExecutor


class AccountSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'test.db'
        self.accounts=Accounts(self.db)
        with store.connect(self.db) as conn:
            for uid in ('api-demo','api-demo2','api-other'):
                store.create_user(conn,uid,uid,'synthetic-password',quota_total=10)
            for uid in ('api-demo','api-demo2'):store.set_user_search_key(conn,uid,'synthetic-demo')
            store.set_settings(conn,{'search_api_key':'synthetic-global-setting'})
            conn.commit()
        self.cookies={uid:'ub_account='+self.accounts.login(uid) for uid in ('api-demo','api-demo2','api-other')}
        self.env=patch.dict(os.environ,{'UNDERBOX_DB':str(self.db),'UNDERBOX_PASSWORD':'',
            'UNDERBOX_GATEWAY_WORKER':'1','SEARCH_PROVIDER':'zhipu','SEARCH_API_KEY':'synthetic-shared',
            'BRAVE_SEARCH_API_KEY':'','LLM_PROVIDER':'deepseek','DEEPSEEK_API_KEY':'synthetic-ds'})
        self.env.start()
        self.barrier=None;self.observed={};self.lock=threading.Lock()
        def work(uid,events,holder):
            if self.barrier:self.barrier.wait(timeout=5)
            with UsageExecutor(max_workers=2) as pool:
                keys=list(pool.map(lambda _:build_searcher().api_key,range(4)))
            with self.lock:self.observed[uid]=(keys,build_llm().api_key)
            # Never send raw credentials over the response, even in this test handler.
            events.put({'done':True,'report':{'account':uid}})
        class Worker(serve.Handler):
            def do_POST(handler):handler._sse(work,(handler.uid(),),'')
        self.worker=ThreadingHTTPServer(('127.0.0.1',0),Worker)
        self.gateway=ThreadingHTTPServer(('127.0.0.1',0),account_handler(serve.Handler,self.accounts,self.worker.server_port))
        for server in (self.worker,self.gateway):threading.Thread(target=server.serve_forever,daemon=True).start()
    def tearDown(self):
        for server in (self.gateway,self.worker):server.shutdown();server.server_close()
        self.env.stop();self.tmp.cleanup()
        with serve._STATE_LOCK:
            for uid in self.cookies:serve._STATES.pop(uid,None)
    def request(self,uid,method='POST',path='/api/verify/stream',headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=8)
        try:
            conn.request(method,path,body=b'{}' if method=='POST' else None,
                         headers={'Cookie':self.cookies[uid],**(headers or {})})
            res=conn.getresponse();return res.status,res.read()
        finally:conn.close()
    def test_concurrent_demo_and_other_account_use_separate_search_keys(self):
        self.barrier=threading.Barrier(3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(self.request,self.cookies))
        self.assertTrue(all(status==200 for status,_ in results))
        self.assertEqual(self.observed['api-demo'],(['synthetic-demo']*4,'synthetic-ds'))
        self.assertEqual(self.observed['api-demo2'],(['synthetic-demo']*4,'synthetic-ds'))
        self.assertEqual(self.observed['api-other'],(['synthetic-shared']*4,'synthetic-ds'))
        self.assertIsNone(search_credentials())
        for _,raw in results:
            self.assertNotIn(b'synthetic-demo',raw);self.assertNotIn(b'synthetic-ds',raw)
        with store.connect(self.db) as conn:
            self.assertEqual(store.get_settings(conn)['search_api_key'],'synthetic-global-setting')
            self.assertTrue(all(u['quota_used']==0 for u in store.list_users(conn)))
    def test_spoofed_identity_cannot_borrow_another_accounts_key(self):
        self.assertEqual(self.request('api-other',headers={'X-Underbox-User':'api-demo'})[0],409)
        self.assertEqual(self.observed,{})
        status,raw=self.request('api-demo',method='GET',path='/api/session')
        self.assertEqual(status,200);self.assertNotIn(b'synthetic-demo',raw)
        self.assertNotIn('api_key',json.loads(raw)['user'])
    def test_invalid_bound_config_fails_instead_of_falling_back_to_shared_key(self):
        with store.connect(self.db) as conn:
            conn.execute("UPDATE user_search_keys SET api_key='' WHERE user_id='api-demo'");conn.commit()
        self.assertEqual(self.request('api-demo')[0],503)
        self.assertEqual(self.observed,{})
    def test_binding_survives_database_reopen_and_schema_init(self):
        store.init(self.db)
        with store.connect(self.db) as conn:
            self.assertEqual(store.user_search_key(conn,'api-demo'),'synthetic-demo')
            self.assertIsNone(store.user_search_key(conn,'api-other'))
            with self.assertRaises(ValueError):store.set_user_search_key(conn,'absent','synthetic-key')

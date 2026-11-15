import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from jobs import Jobs
from test_adapter import AdapterTest

class JobTests(unittest.TestCase):
    def test_progress_result_expiry_and_delete(self):
        event=threading.Event()
        jobs=Jobs(runner=lambda *a: (event.wait(3) and {'state':'completed','result':{'mode':'live'}}),ttl=0.05)
        entry=jobs.submit('虚构测试', True)
        with self.assertRaises(RuntimeError): jobs.submit('另一个',True)
        event.set()
        for _ in range(100):
            if jobs.status(entry['job_id'])['state']!='running': break
            time.sleep(.005)
        self.assertEqual(jobs.status(entry['job_id'])['result']['mode'],'live')
        time.sleep(.06)
        with self.assertRaises(KeyError): jobs.status(entry['job_id'])
    def test_failure_is_not_empty_evidence(self):
        jobs=Jobs(runner=lambda *a: {'state':'failed','error':'provider_failed'})
        key=jobs.submit('虚构测试',True)['job_id']
        for _ in range(100):
            if jobs.status(key)['state']!='running': break
            time.sleep(.005)
        self.assertEqual(jobs.status(key)['state'],'failed')
        self.assertNotIn('result',jobs.status(key))
        self.assertEqual(jobs.delete(key),{'deleted':True})
    def test_requires_consent(self):
        with self.assertRaises(ValueError): Jobs().submit('虚构测试',False)

class LiveAuthTests(AdapterTest):
    def test_fixture_key_cannot_access_live(self):
        import http.client
        c=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port)
        with patch.dict(os.environ,{'UNDERBOX_ICT_LIVE_TOKEN':'y'*32}):
            c.request('POST','/v1/live/submit',json.dumps({'resume_text':'x','consent':True}),{'Authorization':'Bearer '+'x'*32})
            r=c.getresponse()
            self.assertEqual(r.status,401)
            r.read()
        c.close()

class ActionAPITests(AdapterTest):
    def test_wrapper_requires_live_key_and_valid_action(self):
        import http.client
        def send(payload, token='y'*32):
            c=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port)
            c.request('POST','/v1/live/action',json.dumps({'request_json':json.dumps(payload)}),{'Authorization':'Bearer '+token})
            r=c.getresponse(); status=r.status; r.read(); c.close(); return status
        with patch.dict(os.environ,{'UNDERBOX_ICT_LIVE_TOKEN':'y'*32}):
            self.assertEqual(send({'action':'status','job_id':'absent'}),404)
            self.assertEqual(send({'action':'submit','resume_text':'虚构','consent':False}),400)
            self.assertEqual(send({'action':'shell','command':'id'}),400)
            self.assertEqual(send({'action':'status','job_id':'absent'},'x'*32),401)

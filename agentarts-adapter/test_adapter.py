import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from server import Handler, evaluate

class AdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.httpd.token = 'x' * 32
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
    def request(self, data, token='x' * 32):
        c = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=10)
        c.request('POST', '/v1/fixture/verify', json.dumps(data), {'Authorization': 'Bearer ' + token})
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        return r.status, body
    def test_existing_rule_engine_and_fictional_marker(self):
        for case_id in ('c04',):
            result = evaluate(case_id)
            self.assertEqual(result['mode'], 'fictional_replay')
            self.assertEqual(result['case']['claim']['id'], case_id)
            self.assertEqual(result['case']['claim']['category'], '竞赛')
            self.assertTrue(result['case']['status'])
    def test_auth_and_allowlist(self):
        self.assertEqual(self.request({'case_id': 'c04'}, 'wrong')[0], 401)
        self.assertEqual(self.request({'case_id': 'c01'})[0], 400)
        self.assertEqual(self.request({'case_id': 'c04', 'evidences': []})[0], 400)
        status, body = self.request({'case_id': 'c04'})
        self.assertEqual(status, 200)
        self.assertEqual(body['mode'], 'fictional_replay')

if __name__ == '__main__':
    unittest.main()

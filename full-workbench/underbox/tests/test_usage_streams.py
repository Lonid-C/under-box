"""Exercise real HTTP/SSE handlers with concurrent synthetic accounts (no provider calls)."""
import concurrent.futures
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import serve
from app.usage import METER, UsageExecutor

class StreamUsageTests(unittest.TestCase):
    def test_concurrent_sse_streams_have_separate_usage_reports_and_release_buckets(self):
        barrier=threading.Barrier(2)
        def work(count,events,holder):
            barrier.wait(timeout=3)
            with UsageExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: METER.record_search('search_std'),range(count)))
            holder['oby']=object()
            events.put({'done':True,'report':{'ownerCount':count}})
        class H(serve.Handler):
            def do_POST(self): self._sse(work,(int(self.headers['X-Count']),),'')
        server=ThreadingHTTPServer(('127.0.0.1',0),H)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def request(uid,count):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=6)
            conn.request('POST','/test',headers={'X-Underbox-User':uid,'X-Count':str(count)})
            res=conn.getresponse(); raw=res.read().decode();conn.close()
            self.assertEqual(res.status,200)
            return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ')]
        try:
            before=set(METER._buckets)
            with patch.object(serve,'record_gateway_usage') as ledger:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    fa=pool.submit(request,'usage-test-a',3); fb=pool.submit(request,'usage-test-b',7)
                    a,b=fa.result(),fb.result()
            for chunks,count in [(a,3),(b,7)]:
                final=next(e for e in chunks if e.get('done'))
                self.assertEqual(final['usage']['glm']['searches'],count)
                self.assertEqual(final['usage']['glm']['searchYuan'],count*.01)
                self.assertTrue(all(e['usage']['glm']['searches']<=count for e in chunks if 'usage' in e))
            self.assertEqual(serve.user_state('usage-test-a')['presentation'],{'ownerCount':3})
            self.assertEqual(serve.user_state('usage-test-b')['presentation'],{'ownerCount':7})
            self.assertEqual(set(METER._buckets),before)
            self.assertEqual({c.args[2] for c in ledger.call_args_list},{'usage-test-a','usage-test-b'})
        finally:
            server.shutdown();server.server_close()
            with serve._STATE_LOCK:
                serve._STATES.pop('usage-test-a',None);serve._STATES.pop('usage-test-b',None)

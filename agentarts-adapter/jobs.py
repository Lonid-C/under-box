import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from live_verify import validate_request

class Jobs:
    def __init__(self, runner=None, ttl=1800):
        self.runner = runner or self._run
        self.ttl = ttl
        self.lock = threading.Lock()
        self.items = {}
        self.last_start = 0
        threading.Thread(target=self._janitor,daemon=True).start()
    def _cleanup(self):
        for key, item in list(self.items.items()):
            if time.monotonic() - item['created'] > self.ttl and item['state'] != 'running':
                del self.items[key]
    def _janitor(self):
        while True:
            time.sleep(30)
            with self.lock:
                self._cleanup()
    def submit(self, text, consent):
        validate_request(text, consent)
        with self.lock:
            self._cleanup()
            if any(i['state'] == 'running' for i in self.items.values()):
                raise RuntimeError('busy')
            if len(self.items) >= 20 or time.monotonic() - self.last_start < 30:
                raise RuntimeError('rate_limited')
            key = secrets.token_urlsafe(24)
            self.last_start = time.monotonic()
            self.items[key] = {'created': self.last_start, 'state': 'running'}
        threading.Thread(target=self._work, args=(key,text,consent), daemon=True).start()
        return {'job_id':key,'state':'running','poll_after_seconds':10,'expires_in_seconds':self.ttl}
    def _work(self, key, text, consent):
        try:
            result = self.runner(text, consent)
            if result.get('state') not in ('completed','failed'):
                raise ValueError('invalid_worker_response')
        except subprocess.TimeoutExpired:
            result = {'state':'failed','error':'verification_timeout'}
        except Exception:
            result = {'state':'failed','error':'worker_failed'}
        with self.lock:
            if key in self.items:
                self.items[key].update(result)
    def status(self, key):
        if not isinstance(key,str) or len(key) > 100:
            raise ValueError('invalid_job_id')
        with self.lock:
            self._cleanup()
            if key not in self.items:
                raise KeyError('job_not_found_or_expired')
            item = self.items[key]
            return {'job_id':key, 'elapsed_seconds':round(time.monotonic()-item['created']), **{k:v for k,v in item.items() if k != 'created'}}
    def delete(self,key):
        with self.lock:
            if key not in self.items:
                raise KeyError('job_not_found_or_expired')
            if self.items[key]['state'] == 'running':
                raise RuntimeError('job_still_running')
            del self.items[key]
        return {'deleted':True}
    @staticmethod
    def _run(text,consent):
        # Parent owns the entire temp tree, also cleans it after forced timeout.
        with tempfile.TemporaryDirectory(prefix='underbox-ict-job-') as tmp:
            env = {**os.environ, 'TMPDIR':tmp,'PYTHONDONTWRITEBYTECODE':'1'}
            proc = subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('worker.py'))],
                input=json.dumps({'resume_text':text,'consent':consent}),text=True,
                capture_output=True,timeout=480,env=env)
            if proc.returncode:
                return {'state':'failed','error':'worker_failed'}
            return json.loads(proc.stdout)

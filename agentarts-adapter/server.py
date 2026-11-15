#!/usr/bin/env python3
"""Loopback-only API for fixture replay and isolated live verification jobs."""
from __future__ import annotations
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from jobs import Jobs
JOBS = Jobs()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'open_box'))
from fixtures.build_fixture import build

ALLOWED = {'c04'}

def evaluate(case_id: str) -> dict:
    if case_id not in ALLOWED:
        raise ValueError('case_id must be c04')
    report = build()
    item = next((v for v in report.claims if v.claim.id == case_id), None)
    if item is None or item.claim.category != '竞赛':
        raise ValueError('configured competition case is unavailable')
    return {'mode': 'fictional_replay', 'notice': '虚构数据与固定证据回放；未执行在线检索，结果仅用于人工复核。',
            'rule_engine': 'open_box.app.judge.assess', 'case': item.model_dump()}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Do not log request bodies or credentials.
    def reply(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'ok':True,'mode':'ict_adapter','capabilities':['fictional_replay','live_text_jobs']})
        else:
            self.reply(404, {'error': 'not_found'})
    def do_POST(self):
        live = self.path in ('/v1/live/submit','/v1/live/status','/v1/live/delete','/v1/live/action')
        if self.path != '/v1/fixture/verify' and not live:
            return self.reply(404, {'error':'not_found'})
        expected = os.environ.get('UNDERBOX_ICT_LIVE_TOKEN','') if live else self.server.token
        supplied = self.headers.get('Authorization','')
        if not expected or not hmac.compare_digest(supplied.encode(), ('Bearer ' + expected).encode()):
            return self.reply(401, {'error':'unauthorized'})
        try:
            self.connection.settimeout(10)
            length = int(self.headers.get('Content-Length','0'))
            if length <= 0 or length > (262144 if live else 1024):
                return self.reply(400, {'error':'invalid_body_length'})
            if self.headers.get('Transfer-Encoding'):
                return self.reply(400, {'error':'unsupported_encoding'})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                raise ValueError('invalid_request')
            route = self.path
            if route == '/v1/live/action':
                if set(body) != {'request_json'} or not isinstance(body['request_json'],str):
                    raise ValueError('expected_request_json')
                body = json.loads(body['request_json'])
                if not isinstance(body,dict) or body.get('action') not in ('submit','status','delete'):
                    raise ValueError('invalid_action')
                # The full edition retains the exact prepared claims and report on-server.
                # Existing plain-text integrations keep using the original isolated worker.
                full = (isinstance(body.get('workbench_request'),str) or str(body.get('job_id','')).startswith('wb_')
                        or str(body.get('resume_text','')).startswith('UNDERBOX-WORKBENCH/1 '))
                if full:
                    import full_bridge
                    return self.reply(200,full_bridge.forward(body))
                route = '/v1/live/' + body.pop('action')
            if route == '/v1/fixture/verify':
                if set(body) != {'case_id'} or not isinstance(body['case_id'],str):
                    raise ValueError('expected_only_case_id')
                result = evaluate(body['case_id'])
            elif route == '/v1/live/submit':
                if set(body) != {'resume_text','consent'}:
                    raise ValueError('expected_resume_text_and_consent')
                result = JOBS.submit(body['resume_text'],body['consent'])
            else:
                if set(body) != {'job_id'} or not isinstance(body['job_id'],str):
                    raise ValueError('expected_job_id')
                result = JOBS.status(body['job_id']) if route.endswith('/status') else JOBS.delete(body['job_id'])
                if route.endswith('/status'):
                    for _ in range(15):
                        if result['state'] != 'running':
                            break
                        time.sleep(1)
                        result = JOBS.status(body['job_id'])
        except KeyError:
            return self.reply(404, {'error':'job_not_found_or_expired'})
        except RuntimeError as exc:
            return self.reply(429, {'error':str(exc)})
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {'error':'invalid_request_or_missing_consent'})
        except Exception:
            return self.reply(500, {'error':'verification_service_failed'})
        if live and 'result' in result:
            result = {**result, 'result_json':json.dumps(result['result'],ensure_ascii=False)}
            result.pop('result', None)
        self.reply(200,result)

def run():
    token = os.environ.get('UNDERBOX_ICT_TOKEN', '')
    if len(token) < 32:
        raise SystemExit('UNDERBOX_ICT_TOKEN must be at least 32 characters')
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('UNDERBOX_ICT_PORT', '18890'))), Handler)
    server.token = token
    server.serve_forever()

if __name__ == '__main__':
    run()

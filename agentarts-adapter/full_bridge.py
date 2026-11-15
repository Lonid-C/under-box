"""Private server-side handoff for exact selected scopes; no user data in logs."""
import json,os,urllib.request,urllib.error

def forward(data):
    token=os.environ.get('UNDERBOX_ICT_BROKER_TOKEN','')
    if not token:raise RuntimeError('full_workbench_not_configured')
    req=urllib.request.Request('http://127.0.0.1:'+os.environ.get('UNDERBOX_ICT_BROKER_PORT','18904')+'/tool',
       data=json.dumps(data,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=25) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code==404:raise KeyError('job_not_found_or_expired')
        raise ValueError('invalid_workbench_ticket_or_scope')
    except (urllib.error.URLError,TimeoutError):raise RuntimeError('full_workbench_unavailable')

"""Run as server administrator. Select only provider settings; never print secrets."""
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'open_box'))
# Prevent importing app from automatically loading the production environment.
os.environ['UNDERBOX_ENV_FILE']='/dev/null'
from app.env import parse_env_text
source = None
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        pairs = proc.joinpath('environ').read_bytes().split(b'\0')
        active = {k.decode():v.decode() for x in pairs if b'=' in x for k,v in [x.split(b'=',1)]}
    except (OSError,UnicodeError):
        continue
    if active.get('UNDERBOX_GATEWAY_WORKER') == '1':
        source = active
        break
if source is None:
    raise SystemExit('running Underbox worker configuration unavailable; do not fall back to stale credentials')
allowed={'DEEPSEEK_API_KEY','LLM_API_KEY','LLM_MODEL','LLM_PROVIDER','LLM_ENDPOINT',
    'LLM_THINKING','SEARCH_API_KEY','SEARCH_PROVIDER','SEARCH_ENGINE','SEARCH_ENDPOINT',
    'SEARCH_ENGINE_OPEN','SEARCH_ENGINE_OPEN_ALTERNATE','SEARCH_MIN_INTERVAL'}
config={k:v for k,v in source.items() if k in allowed}
if not (config.get('DEEPSEEK_API_KEY') or config.get('LLM_API_KEY')) or not config.get('SEARCH_API_KEY'):
    raise SystemExit('model or search credential unavailable')
config['UNDERBOX_ENV_FILE']='/dev/null'
config['CLAIM_WORKERS']='1'
config['LLM_CONCURRENCY']='2'
p=Path('/etc/underbox-ict/live.env')
if p.exists():
    prior=parse_env_text(p.read_text())
    config['UNDERBOX_ICT_LIVE_TOKEN']=prior.get('UNDERBOX_ICT_LIVE_TOKEN') or secrets.token_hex(32)
else: config['UNDERBOX_ICT_LIVE_TOKEN']=secrets.token_hex(32)
def quote(s):
    if '\n' in s or '\r' in s: raise ValueError('multiline configuration unsupported')
    return '"'+s.replace('\\','\\\\').replace('"','\\"')+'"'
p.write_text('\n'.join(k+'='+quote(v) for k,v in config.items())+'\n')
p.chmod(0o600)
print('Isolated provider configuration prepared; credentials not printed.')

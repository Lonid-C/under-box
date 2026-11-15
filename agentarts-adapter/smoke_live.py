"""Synthetic-only live smoke test. Does not print candidate text or credentials."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'open_box'))
os.environ['UNDERBOX_ENV_FILE']='/dev/null'
from app.env import parse_env_text
os.environ.update(parse_env_text(Path('/etc/underbox-ict/live.env').read_text()))
from jobs import Jobs
text='个人简历（虚构测试数据）\n姓名：林昱和\n教育经历\n晴川大学计算机学院，本科在读，2022年9月至2026年6月。\n竞赛经历\n2023年11月，ICPC亚洲区域赛晴川站铜奖，队伍Trailing Zeros。\n求职方向：软件工程师。'
result=Jobs._run(text,True)
print(json.dumps({'state':result['state'],'error':result.get('error'),'mode':result.get('result',{}).get('mode'),'usage':result.get('result',{}).get('usage'),'claim_count':len(result.get('result',{}).get('report',{}).get('claims',[]))},ensure_ascii=False))
Path('/tmp/underbox-ict-live-smoke.json').write_text(json.dumps(result,ensure_ascii=False))
Path('/tmp/underbox-ict-live-smoke.json').chmod(0o600)

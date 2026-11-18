#!/usr/bin/env python3
"""Private, headless A/B runner; no listener, production write or promotion API.

prepare once -> run both independent processes -> durable partial results.
The 900-second comparison clock starts after the shared split, as in the UI.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'open_box'))


def private_write(path,data):
    path=Path(path);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+str(threading.get_ident())+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(data,stream,ensure_ascii=False,indent=2)
    os.replace(tmp,path)


def live_environment(output):
    """Read existing credentials in memory, isolate all experiment persistence."""
    output=Path(output).resolve()
    protected=[Path(p).resolve() for p in ('/var/lib/underbox-ict-full','/opt/underbox-huawei-ict')]
    if any(output==p or p in output.parents for p in protected):
        raise ValueError('experiment_output_must_not_be_production')
    listener=subprocess.check_output(['ss','-ltnp','sport = :18906'],text=True)
    match=re.search(r'pid=(\d+)',listener)
    if not match:raise RuntimeError('production_worker_not_found_for_authorized_credentials')
    env={}
    for item in (Path('/proc')/match[1]/'environ').read_bytes().split(b'\0'):
        key,sep,value=item.partition(b'=')
        if sep:env[key.decode()]=value.decode()
    for k,v in env.items():
        if k.startswith(('UNDERBOX_','DEEPSEEK_','LLM_','SEARCH_','ZHIPU_','CLAIM_')):os.environ[k]=v
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.environ.update(UNDERBOX_DB=str(output/'experiment-unused.db'),OPEN_BOX_ROOT=str(ROOT/'open_box'),
        ARCHIVE_CACHE=str(output/'archive-cache.json'),
        UNDERBOX_ICT_ENABLE='1',UNDERBOX_ICT_RECALL='1',UNDERBOX_ICT_UNLIMITED='1',
        UNDERBOX_ICT_GLM_ONLY='1',UNDERBOX_ICT_COST_BUDGET='0',UNDERBOX_ENV_FILE='/dev/null',MODE='live')
    return {'claimWorkers':os.environ.get('CLAIM_WORKERS','0'),
        'modelConcurrency':os.environ.get('LLM_CONCURRENCY','10'),
        'searchEngine':os.environ.get('SEARCH_ENGINE'),
        'openSearchEngine':os.environ.get('SEARCH_ENGINE_OPEN'),
        'productionSearchPolicyUnchanged':True,'newNetworkListeners':0}


def metered_llm():
    from app.llm import build_llm
    from app.pipeline import BoundedLLM,_llm_limit
    return BoundedLLM(build_llm('deepseek'),_llm_limit())


def split_sections(text):
    """Chapter boundaries avoid the legacy per-call 20-claim prompt ceiling."""
    headings=list(re.finditer(r'(?m)^\s*(?:教育背景|校园经历|竞赛获奖|科研情况|专利|会议|其他文章|参与项目|实习经历)\s*[:：]?\s*$',text))
    if not headings:return [text]
    chunks=[]
    for i,m in enumerate(headings):
        block=text[m.start():headings[i+1].start() if i+1<len(headings) else len(text)].strip()
        if len(block)>30:chunks.append(block)
    return chunks or [text]


def prepare(args):
    live_environment(Path(args.output).parent)
    os.environ["UNDERBOX_ICT_PARALLEL_AGENTS"] = "0"
    os.environ["UNDERBOX_ICT_PIPELINE_NEXT"] = "1" if args.pipeline == "next" else "0"
    from app.pipeline import prepare_resume
    from app.usage import METER
    from app.cost_budget import CostBudget,bind
    scope=METER.open_scope();started=time.monotonic();ledger=CostBudget(cloud_allowance=0)
    with METER.bind(scope),bind(ledger):
        model=metered_llm()
        if getattr(args,'section_split',False):
            from app.parse import parse_document
            from app.split import split_claims
            from app.pipeline import PreparedResume,skip_categories
            from app.strategy import ResumeProfile,add_admission_source_hints,add_competition_school_hints
            from app.identity import with_candidate_identity_context
            from app.usage import UsageExecutor
            seed=json.loads(Path(args.reuse_profile).read_text())
            doc=parse_document(args.input);name=seed['name'];profile=ResumeProfile.from_dict(seed['profile'])
            sections=split_sections(doc.safe_text)
            with UsageExecutor(max_workers=2) as pool:
                batches=list(pool.map(lambda s:split_claims('候选人姓名：'+name+'\n'+s,model),sections))
            items=[c for batch in batches for c in batch]
            for i,c in enumerate(items,1):c.id=f'c{i:02d}'
            items=with_candidate_identity_context(add_competition_school_hints(add_admission_source_hints(items)),name)
            skip=skip_categories();value=PreparedResume(str(args.input),doc,model,profile,
                [c for c in items if c.category not in skip],name,[c for c in items if c.category in skip])
        else:value=prepare_resume(args.input,llm=model,progress=lambda *a,**k:None)
    text=Path(args.input).read_text()
    snapshot={'name':value.name,'profile':asdict(value.profile),
        'claims':[c.model_dump(mode='json') for c in value.claims],
        'skipped':[c.model_dump(mode='json') for c in value.skipped],
        'text':value.doc.safe_text,'risks':[r.model_dump(mode='json') for r in value.doc.risks],
        'inputSha256':hashlib.sha256(text.encode()).hexdigest(),
        'preparedSeconds':round(time.monotonic()-started,3),'usage':METER.snapshot(scope),
        'cost':ledger.snapshot(),'scope':'shared preprocessing, not an A/B outcome',
        'sectionSplit':bool(getattr(args,'section_split',False)),
        'sharedPreflightCostCny':seed['cost']['estimatedCny'] if getattr(args,'section_split',False) else 0}
    private_write(args.output,snapshot);METER.release_scope(scope)
    print(json.dumps({'preparedClaims':len(value.claims),'skippedClaims':len(value.skipped),
        'categories':dict(Counter(c.category for c in value.claims)),
        'seconds':snapshot['preparedSeconds'],'estimatedCny':snapshot['cost']['estimatedCny']},ensure_ascii=False))


def worker(args):
    output=Path(args.output);config=live_environment(output)
    improved = args.pipeline == "next"
    os.environ["UNDERBOX_ICT_PARALLEL_AGENTS"] = "0"
    os.environ["UNDERBOX_ICT_PIPELINE_NEXT"] = "1" if improved else "0"
    config["pipeline"] = args.pipeline
    from app import collect,pipeline
    from app.data_sources import AgentRun
    from app.dual_search_experiment import (Deadline,DualSearchExperiment,
        DeadlineSearcher,DeadlineFetcher,DeadlineLLM,summarize_claims)
    from app.deepseek_search import DeepSeekSearcher
    from app.cost_budget import CostBudget,bind
    from app.schema import Claim,VerifiedClaim,InputRisk
    from app.parse import ParsedDocument
    from app.strategy import ResumeProfile
    from app.search import build_searcher,PageFetcher,read_remaining
    from app.usage import METER
    raw=Path(args.input).read_bytes();snapshot=json.loads(raw)
    claims=[Claim.model_validate(c) for c in snapshot['claims']]
    skipped=[Claim.model_validate(c) for c in snapshot['skipped']]
    scope=METER.open_scope();ledger=CostBudget(cloud_allowance=0)
    completed={};evidence={};runs={};network=Counter();lock=threading.RLock();stop=threading.Event()
    save_lock=threading.RLock();finalized=threading.Event()
    started_wall=args.start_at;deadline=Deadline(end=time.monotonic()+max(0,started_wall+args.seconds-time.time()))
    trace_path=output/'trace.jsonl';fd=os.open(trace_path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    trace_file=os.fdopen(fd,'a',buffering=1);trace_lock=threading.RLock()

    def trace(event):
        with trace_lock:trace_file.write(json.dumps({'at':time.time(),**event},ensure_ascii=False)+'\n')

    def build_checkpoint(*,final=False,error=None):
        with lock:
            finished={k:dict(v) for k,v in completed.items()}
            pending={k:{'claim':v['claim'],'items':list(v['items'])} for k,v in evidence.items()}
            run_stats={cid:run.snapshot() for cid,run in runs.items()}
            net=dict(network)
        result=[];expired=deadline.remaining()<=0
        for c in claims:
            if c.id in finished:
                vc=VerifiedClaim.model_validate(finished[c.id])
                execution=(vc.verification_agent or {}).get('sourceAccess',{}).get('execution',{})
                if not improved and execution.get('state')=='time_limited':
                    vc.retrieval_status='timeout';vc.needs_human=True
                    vc.next_step='整份核验达到15分钟总时限；保留已有证据，未覆盖要素因超时停止。'
            else:
                item=pending.get(c.id)
                if item:
                    from app.schema import Evidence
                    enriched=Claim.model_validate(item['claim'])
                    vc=pipeline.judge.assess(enriched,[Evidence.model_validate(e) for e in item['items']],search_exhausted=True)
                else:vc=pipeline.judge.assess(c,[],search_exhausted=True)
                vc.retrieval_status='timeout' if expired else 'partial' if c.id in run_stats else 'not_attempted'
                vc.needs_human=True
                if expired:
                    vc.next_step = ('整份核验达到总时限，本条尚未启动。' if c.id not in run_stats
                                    else '整份核验达到总时限，本条已停止查询或读取。已取得的证据保留。')
                else:
                    vc.next_step = '后台对照测试正在运行，当前只是中间快照。'
                if c.id in run_stats:vc.verification_agent={'sourceAccess':run_stats[c.id]}
            result.append(vc)
        with METER.bind(scope):usage=METER.snapshot(scope)
        state={'arm':args.arm,'final':final,'timedOut':expired,'completedClaims':len(finished),
            'startedClaims':len(set(run_stats) | set(finished)), 'notStartedClaims':len(claims)-len(set(run_stats) | set(finished)),
            'runningClaims':len(set(run_stats)-set(finished)),
            'elapsedSeconds':round(max(0,time.time()-started_wall),3),
            'maxSeconds':args.seconds,'startAt':started_wall,'snapshotSha256':hashlib.sha256(raw).hexdigest(),
            'config':config,'metrics':summarize_claims(result),'usage':usage,'cost':ledger.snapshot(),
            'networkRequests':net,'error':error,'claims':[c.model_dump(mode='json') for c in result]}
        private_write(output/'result.json',state)
        # Heartbeats contain counts only; CV/query snippets stay in private files.
        private_write(output/'status.json',{k:state[k] for k in state if k not in {'claims'}})

    def checkpoint(*,final=False,error=None):
        with save_lock:
            if finalized.is_set() and not final:return
            if final:finalized.set()
            build_checkpoint(final=final,error=error)

    original_init=AgentRun.__init__
    def init_run(run,*a,**kw):
        original_init(run,*a,**kw)
        with lock:runs[run.claim.id]=run
    AgentRun.__init__=init_run
    original_finish=AgentRun.finish
    def finish_run(run,vc):
        row=original_finish(run,vc);vc.verification_agent=row
        with lock:completed[vc.claim.id]=vc.model_dump(mode='json')
        checkpoint();return row
    AgentRun.finish=finish_run
    def retain(claim, ev):
        with lock:
            row=evidence.setdefault(claim.id,{'claim':claim.model_dump(mode='json'),'items':[]})
            row['items'].append(ev.model_dump(mode='json'))
    collect.EVIDENCE_OBSERVER.set(retain)

    # All network calls, including retries/public metadata/robots, check this
    # arm's shared deadline. The supervisor also terminates at the hard cutoff.
    import httpx
    def bounded_network(original,method):
        def call(url,*a,**kw):
            deadline.check()
            remaining = min(deadline.remaining(), read_remaining()) if improved else deadline.remaining()
            if remaining <= 0:
                raise httpx.ReadTimeout("本条或本批读取达到执行时限")
            timeout=kw.get('timeout',5)
            if isinstance(timeout,httpx.Timeout):
                kw['timeout']=httpx.Timeout(min(timeout.read or remaining,remaining),
                    connect=min(timeout.connect or remaining,remaining),write=min(timeout.write or remaining,remaining),pool=min(timeout.pool or remaining,remaining))
            else:kw['timeout']=min(float(timeout or remaining),remaining)
            host=urlsplit(str(url)).hostname or ''
            with lock:network[host]+=1
            status=None;start=time.monotonic()
            try:
                response=original(url,*a,**kw);status=response.status_code;return response
            finally:trace({'action':'http','method':method,'host':host,'path':urlsplit(str(url)).path,
                'status':status,'seconds':round(time.monotonic()-start,3)})
        return call
    for method in ('get','post','put','delete','head'):
        setattr(httpx,method,bounded_network(getattr(httpx,method),method))

    def heartbeat():
        while not stop.wait(10):checkpoint()
    def terminate(signum,frame):
        deadline.end=deadline.clock();stop.set()
        try:checkpoint(final=True,error='overall_timeout')
        finally:os._exit(0)
    signal.signal(signal.SIGTERM,terminate)
    signal.signal(signal.SIGINT,terminate)
    while time.time()<started_wall:time.sleep(min(.05,started_wall-time.time()))
    trace({'action':'arm_started','arm':args.arm})
    model=DeadlineLLM(metered_llm(),deadline);primary=build_searcher('zhipu')
    if args.arm=='dual':
        primary=DualSearchExperiment(primary,model,DeepSeekSearcher(),deadline,trace=trace)
    searcher=DeadlineSearcher(primary,deadline)
    reader = PageFetcher(timeout=6, connect_timeout=3) if improved else PageFetcher()
    fetcher=DeadlineFetcher(reader,deadline)
    prepared=pipeline.PreparedResume(resume_path=str(Path(args.input)),
        doc=ParsedDocument(snapshot['text'],[InputRisk.model_validate(r) for r in snapshot['risks']],source='redacted-resume.txt'),
        profile=ResumeProfile.from_dict(snapshot['profile']),name=snapshot['name'],claims=claims,skipped=skipped,llm=model)
    def budget_factory():
        b=collect.Budget();b.max_seconds=max(.001,deadline.remaining());return b
    checkpoint();threading.Thread(target=heartbeat,daemon=True).start()
    try:
        with METER.bind(scope),bind(ledger):
            report=pipeline.verify_prepared(prepared,report_id='experiment-'+args.arm,
                searcher=searcher,fetcher=fetcher,budget_factory=budget_factory,
                progress=lambda *a,**kw:trace({'action':'progress','message':' '.join(map(str,a))}))
        with lock:
            completed.update({vc.claim.id:vc.model_dump(mode='json') for vc in report.claims})
        private_write(output/'report.json',report.model_dump(mode='json'));checkpoint(final=True)
    except Exception as exc:
        checkpoint(final=True,error=type(exc).__name__)
    finally:
        stop.set();METER.release_scope(scope);trace_file.close()


def run(args):
    output=Path(args.output);output.mkdir(mode=0o700,parents=True,exist_ok=True)
    start=time.time()+2;children={};streams=[]
    for arm in ('single','dual'):
        folder=output/arm;folder.mkdir(mode=0o700,exist_ok=False)
        stream=(folder/'worker.log').open('w');os.chmod(folder/'worker.log',0o600);streams.append(stream)
        command=[sys.executable,__file__,'worker','--arm',arm,'--input',args.input,
                 '--output',str(folder),'--seconds',str(args.seconds),'--start-at',str(start),
                 '--pipeline',args.pipeline]
        children[arm]=subprocess.Popen(command,stdout=stream,stderr=stream,start_new_session=True)
    private_write(output/'run.json',{'pids':{k:p.pid for k,p in children.items()},'startAt':start,
        'maxSeconds':args.seconds,'inputSnapshot':str(Path(args.input).resolve()),'sourceRoot':str(ROOT),'pipeline':args.pipeline,
        'mode':'independent processes, same snapshot and deadline, no production writes/listeners'})
    while any(p.poll() is None for p in children.values()):
        if time.time()>=start+args.seconds:
            for p in children.values():
                if p.poll() is None:os.killpg(p.pid,signal.SIGTERM)
            break
        time.sleep(.5)
    for p in children.values():
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
    for s in streams:s.close()
    states={}
    for arm in children:
        p=output/arm/'status.json'
        states[arm]=json.loads(p.read_text()) if p.exists() else {'error':'no_result','exitCode':children[arm].returncode}
    private_write(output/'comparison-status.json',states)
    print(json.dumps(states,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for action in ('prepare','run','worker'):
        p=sub.add_parser(action);p.add_argument('--input',required=True);p.add_argument('--output',required=True)
        p.add_argument('--pipeline',choices=('next','legacy'),default='next')
        if action=='prepare':p.add_argument('--section-split',action='store_true');p.add_argument('--reuse-profile')
        if action!='prepare':p.add_argument('--seconds',type=float,default=900)
        if action=='worker':p.add_argument('--arm',choices=('single','dual'),required=True);p.add_argument('--start-at',type=float,required=True)
    args=parser.parse_args();globals()[args.command](args)


if __name__=='__main__':main()

"""ICT edition integration. Original modules retain evidence and account behavior.

A single-use ticket authorizes one exact selected scope. The full report stays on
this server; AgentArts must invoke RunUnderbox and return its digest
receipt before the browser receives a completed report. No cloud bypass fallback.
"""
from __future__ import annotations
import contextvars,hashlib,hmac,json,os,queue,re,secrets,threading,time,urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import runtime_client

VERSION='ict-full-20261114.1'
USER=contextvars.ContextVar('ict_user',default='local')
LOCK=threading.RLock(); TICKETS={}; ACTIVE=threading.Lock()
ENGINE=None; ORIGINAL_VERIFY=None; ORIGINAL_SPLIT=None
BASE_MARKER='__ICT_BASE__'

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def receipt_for(ticket):
    return {'workbench_receipt':{'request_id':ticket['id'],'report_sha256':hashlib.sha256(canonical(ticket['done']['report'])).hexdigest()},
            'summary':{'claim_count':len(ticket['done']['report'].get('claims',[])),
                       'statuses':[c.get('status') for c in ticket['done']['report'].get('claims',[])]},
            'notice':'完整报告保留在工作台，仅供人工复核；无证据不表示经历不实。'}

def cleanup():
    now=time.monotonic()
    for key in list(TICKETS):
        t=TICKETS[key]
        if now-t['created']>3600 and t['state'] not in ('running',):TICKETS.pop(key,None)

def register(token,ids,materials,events,holder):
    if not 1<=len(set(ids))<=8:raise ValueError('每轮请选择1–8项，可分轮核验。')
    with ENGINE._PREPARED_LOCK:
        entry=ENGINE.PREPARED.get(token)
        if entry is None or entry.get('owner','local')!=USER.get():raise ValueError('拆分结果已过期或不属于当前账户。')
        if entry.get('snapshot'):raise ValueError('比赛实时入口不使用私有文件回放，请上传脱敏材料。')
        prepared=entry.get('prepared')
        if prepared is None:raise ValueError('缺少拆分结果。')
        selected=[c for c in prepared.claims if c.id in ids]
        if len(selected)!=len(set(ids)):raise ValueError('包含无效事项。')
        text='个人简历（选中事项，用户已授权）\n个人信息\n姓名：'+prepared.name+'\n待核验经历\n'+'\n'.join(c.raw_text for c in selected)
        if len(text.encode())>32768:raise ValueError('选中摘录超过32768字节，请缩小范围。')
    with LOCK:
        cleanup()
        if not ACTIVE.acquire(blocking=False):raise ValueError('已有核验正在运行，请等待完成后再试。')
        rid='wb_'+secrets.token_urlsafe(24)
        t={'id':rid,'owner':USER.get(),'created':time.monotonic(),'state':'awaiting','text':'UNDERBOX-WORKBENCH/1 '+rid+'\n'+text,
           'cloud_session':secrets.token_hex(16),'token':token,'ids':list(ids),'materials':materials,'events':events,'holder':holder,
           'context':contextvars.copy_context(),'finished':threading.Event(),'done':None,'error':None,'released':False}
        from app.usage import METER
        scope=METER.mark()['_scope'];t['meter_scope']=scope if METER.retain_scope(scope) else None
        TICKETS[rid]=t
    return t

def release(t):
    with LOCK:
        if not t['released']:
            t['released']=True;ACTIVE.release()
            if t.get('meter_scope'):
                from app.usage import METER
                METER.release_scope(t['meter_scope'])

def execute(t):
    class Capture:
        def put(self,event):
            if event is None:return
            if event.get('done'):
                if event.get('report'):event['report']['sourceFile']=ENGINE.PREPARED.get(t['token'],{}).get('filename','已确认摘录')
                t['done']=event
            else:t['events'].put(event)
    try:
        t['context'].run(ORIGINAL_VERIFY,t['token'],t['ids'],Capture(),t['holder'],materials=t['materials'])
        with LOCK:
            done=t.get('done') or {}
            if done.get('report') and not done.get('failed'):
                t['state']='completed';t['receipt']=receipt_for(t)
            else:t['state']='failed';t['error']=done.get('error') or '核验服务未返回完整报告。'
    except Exception as exc:
        with LOCK:t['state']='failed';t['error']='核验服务异常（'+type(exc).__name__+'）。'
    finally:t['finished'].set();release(t)

def tool_action(data):
    action=data.get('action')
    if action=='submit':
        rid=data.get('workbench_request')
        if not rid:
            marker=re.match(r'^UNDERBOX-WORKBENCH/1 (wb_[A-Za-z0-9_-]+)\n',str(data.get('resume_text','')))
            rid=marker.group(1) if marker else None
        t=TICKETS.get(rid)
        if not t or data.get('consent') is not True:raise ValueError('invalid_workbench_ticket_or_consent')
        supplied=data.get('resume_text')
        if not isinstance(supplied,str) or not hmac.compare_digest(supplied.encode(),t['text'].encode()):raise ValueError('selected_scope_mismatch')
        with LOCK:
            if t['state']=='awaiting':
                t['state']='running'
                t['events'].put({'log':'AgentArts 已实际调用 RunUnderbox，开始核验选中事项。','stage':'agentarts_tool'})
                threading.Thread(target=execute,args=(t,),daemon=True).start()
        return {'job_id':rid,'state':t['state']}
    rid=data.get('job_id');t=TICKETS.get(rid)
    if not t:raise KeyError('job_not_found_or_expired')
    if action=='status':
        t['finished'].wait(14)
        result={'job_id':rid,'state':t['state'],'elapsed_seconds':round(time.monotonic()-t['created'],2)}
        if t['state']=='completed':result['result_json']=json.dumps(t['receipt'],ensure_ascii=False)
        if t['state']=='failed':result['error']=t['error']
        return result
    if action=='delete':
        if t['state']=='running':raise ValueError('job_is_running')
        with LOCK:TICKETS.pop(rid,None)
        release(t);return {'job_id':rid,'state':'deleted'}
    raise ValueError('invalid_action')

class ToolHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        expected=os.environ.get('UNDERBOX_ICT_BROKER_TOKEN','')
        if self.path!='/tool' or not expected or not hmac.compare_digest(self.headers.get('Authorization','').encode(),('Bearer '+expected).encode()):
            return self.reply(401,{'error':'unauthorized'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=65536 or self.headers.get('Transfer-Encoding'):raise ValueError('invalid_body')
            data=json.loads(self.rfile.read(n))
            if not isinstance(data,dict):raise ValueError('invalid_body')
            self.reply(200,tool_action(data))
        except KeyError:self.reply(404,{'error':'job_not_found_or_expired'})
        except (ValueError,TypeError):self.reply(400,{'error':'invalid_ticket_scope_or_action'})
        except Exception:self.reply(500,{'error':'broker_error'})
    def reply(self,code,data):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)

def find_receipt(value,rid,digest,depth=0):
    if depth>14:return False
    if isinstance(value,dict):
        ref=value.get('workbench_receipt')
        if isinstance(ref,dict) and ref.get('request_id')==rid and ref.get('report_sha256')==digest:return True
        return any(find_receipt(v,rid,digest,depth+1) for k,v in value.items() if k not in ('reasoning_content','query','input','arguments','request_json'))
    if isinstance(value,list):return any(find_receipt(v,rid,digest,depth+1) for v in value)
    if isinstance(value,str):
        try:return find_receipt(json.loads(value),rid,digest,depth+1)
        except (ValueError,TypeError):pass
        decoder=json.JSONDecoder()
        for match in re.finditer(r'\{',value[:300000]):
            try:obj,_=decoder.raw_decode(value[match.start():])
            except ValueError:continue
            if find_receipt(obj,rid,digest,depth+1):return True
    return False

def cloud_roundtrip(t):
    # Preserve the deployed tool's existing three-field submit contract.
    # The opaque first line is transport metadata, not a new resume claim.
    payload={'action':'submit','resume_text':t['text'],'consent':True}
    query=('我已获得本次材料处理授权，并同意由华为云AgentArts、服务器DeepSeek及智谱搜索处理以下选中事项。'
           '请调用RunUnderbox提交以下JSON一次，不要修改resume_text（第一行是工作台传输标识，不是经历）。'
           '拿到job_id后用action=status查询至completed，最多20次，不重复submit。完成后直接返回工具实际result_json，失败如实说明。\n'+json.dumps(payload,ensure_ascii=False))
    outputs=[];text='';started=time.monotonic();count=0
    with runtime_client.invoke(query,t['cloud_session']) as response:
        for raw in response:
            if time.monotonic()-started>480:raise TimeoutError('platform_timeout')
            if not raw.startswith(b'data:'):continue
            try:event=json.loads(raw[5:])
            except ValueError:continue
            if not isinstance(event,dict):continue
            count+=1
            if event.get('event') in ('error','failed'):
                t['platform_error']=str(event.get('message') or event.get('error') or '平台执行错误')[:600]
                raise RuntimeError('platform_failed')
            # Discard reasoning. Only public content/summary and completed tool results are inspected.
            event={k:v for k,v in event.items() if k!='reasoning_content'}
            outputs.append(event)
            content=event.get('content')
            if event.get('event')=='message' and isinstance(content,str):text+=content
            elif event.get('event')=='summary_response' and isinstance(content,str):text=content
            if len(text)>1024*1024 or count>30000:raise ValueError('platform_response_too_large')
    t['platform_event_types']=list(dict.fromkeys(str(x.get('event','')) for x in outputs))
    done=t.get('done') or {}
    if t['state']!='completed' or not done.get('report'):raise RuntimeError('platform_did_not_complete_tool')
    digest=hashlib.sha256(canonical(done['report'])).hexdigest()
    if not find_receipt([outputs,text],t['id'],digest):raise RuntimeError('platform_receipt_missing_or_changed')
    return {'session':t['cloud_session'],'request':t['id'],'events':count,'receipt_sha256':digest,'elapsed_seconds':round(time.monotonic()-started,2)}

def verify_selected(token,ids,events,holder,*,materials=None):
    started=time.monotonic();ticket=None
    try:
        ticket=register(token,ids,materials,events,holder)
        events.put({'log':'已确认核验范围，正在请求华为云 AgentArts 编排。'})
        info=cloud_roundtrip(ticket)
        event=dict(ticket['done']);event['report']['sourceFile']=ENGINE.PREPARED.get(token,{}).get('filename','已确认的简历摘录');event['seconds']=round(time.monotonic()-started,1)
        event['report']={**event['report'],'agentarts':info,'edition':'huawei-ict'}
        events.put({'log':'AgentArts 已返回与本轮完整报告匹配的回执。'})
        events.put(event)
    except Exception as exc:
        if ticket and ticket['state']=='running':
            events.put({'log':'平台连接已结束，正在等待本轮后台任务收尾；不会重复提交。'})
            ticket['finished'].wait(600)
        if ticket and ticket['state']=='awaiting':
            ticket['state']='failed';release(ticket)
        codes={'iam_not_configured':'IAM配置缺失','platform_failed':'平台执行错误','platform_did_not_complete_tool':'平台未完成工作台工具任务','platform_receipt_missing_or_changed':'平台回执缺失或不匹配'}
        code=codes.get(str(exc),type(exc).__name__)
        message=str(exc) if isinstance(exc,ValueError) else 'AgentArts 调用未完成（'+code+'），未自动重复提交。'
        events.put({'done':True,'failed':True,'error':message,'seconds':round(time.monotonic()-started,1)})
    finally:events.put(None)

def verify_file(data,filename,events,holder):
    # Preserve the legacy one-step endpoint, still passing its verification through AgentArts.
    class SplitCapture:
        def __init__(self):self.last=None
        def put(self,ev):
            if ev is None:return
            if ev.get('done'):self.last=ev
            else:events.put(ev)
    q=SplitCapture();ORIGINAL_SPLIT(data,filename,q,holder)
    if q.last and q.last.get('split'):
        split=q.last['split'];token=split['token']
        with ENGINE._PREPARED_LOCK:ENGINE.PREPARED[token]['owner']=USER.get()
        verify_selected(token,[c['id'] for c in split['claims']],events,holder)
    else:events.put(q.last or {'done':True,'failed':True,'error':'拆分未完成。'});events.put(None)

def sse(self,target,args,kind=''):
    from app.usage import METER
    from app.api_config import bind_search_credentials
    try:credentials=ENGINE.account_search_credentials(self.uid())
    except Exception:return self._json({'error':'账户搜索配置不可用。'},503)
    uid=self.uid();generation=self._st()['generation'];holder={};events=queue.Queue()
    scope=METER.open_scope(owners=2);base=METER.mark(scope)
    unsubscribe=METER.subscribe(lambda:events.put({'usage':METER.since(base)}),scope)
    class Tracking:
        completed=False
        def put(q,ev):
            if ev and ev.get('done') and not q.completed:
                q.completed=True
                if kind=='split' and ev.get('split',{}).get('token'):
                    with ENGINE._PREPARED_LOCK:
                        entry=ENGINE.PREPARED.get(ev['split']['token'])
                        if entry is not None:entry['owner']=uid
                ev['usage']=METER.since(base);saved_id=ENGINE.record_gateway_usage(kind,ev,uid)
                if saved_id and holder.get('oby') is not None:save_context(saved_id,holder)
                if 'report' in ev:
                    with ENGINE._STATE_LOCK:
                        state=ENGINE._STATES.get(uid)
                        if state and state['generation']==generation:state.update(presentation=ev['report'],oby=holder.get('oby'),profile=holder.get('profile'))
            events.put(ev)
    tracker=Tracking()
    def run():
        ident=USER.set(uid)
        try:
            with bind_search_credentials(credentials):METER.run_in_scope(scope,target,*args,tracker,holder)
        except Exception as exc:
            tracker.put({'done':True,'failed':True,'error':'处理失败（'+type(exc).__name__+'）。'});events.put(None)
        finally:USER.reset(ident)
    threading.Thread(target=run,daemon=True).start()
    try:
        self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Accel-Buffering','no');self.send_header('Connection','close');self.close_connection=True;self.end_headers()
        while True:
            try:ev=events.get(timeout=10)
            except queue.Empty:ev={'ping':True}
            if ev is None:break
            self.wfile.write(('data: '+json.dumps(ev,ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
            if ev.get('done'):break
    except (BrokenPipeError,ConnectionResetError):pass
    finally:unsubscribe();METER.release_scope(scope)

def context_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS ict_report_context(report_id INTEGER PRIMARY KEY REFERENCES report_history(id) ON DELETE CASCADE,report_json TEXT NOT NULL,profile_json TEXT NOT NULL)")

def save_context(report_id,holder):
    import dataclasses,store
    rep=holder['oby'];profile=holder.get('profile')
    profile_json=dataclasses.asdict(profile) if profile is not None and dataclasses.is_dataclass(profile) else {}
    with store.connect(os.environ['UNDERBOX_DB']) as conn:
        context_table(conn)
        conn.execute("INSERT OR REPLACE INTO ict_report_context VALUES(?,?,?)",(report_id,json.dumps(rep.model_dump(mode='json'),ensure_ascii=False),json.dumps(profile_json,ensure_ascii=False)))
        conn.commit()

def activate_history(handler,rid):
    import store
    from app.schema import Report
    from app.strategy import ResumeProfile
    with store.connect(os.environ['UNDERBOX_DB']) as conn:
        report=store.get_report(conn,handler.uid(),rid)
        if report is None:return handler._json({'error':'历史报告不存在或不属于当前账户。'},404)
        context_table(conn);row=conn.execute("SELECT * FROM ict_report_context WHERE report_id=?",(rid,)).fetchone()
    if row is None:return handler._json({'error':'此历史记录缺少问答上下文，请仅通过历史导出查看。'},409)
    raw=Report.model_validate(json.loads(row['report_json']));profile=ResumeProfile(**json.loads(row['profile_json']))
    with ENGINE._STATE_LOCK:
        st=ENGINE._STATES.setdefault(handler.uid(),{'generation':0})
        st.update(presentation=report,oby=raw,profile=profile,generation=st['generation']+1)
    return handler._json({'report':report})

def install(engine):
    global ENGINE,ORIGINAL_VERIFY,ORIGINAL_SPLIT
    ENGINE=engine;ORIGINAL_VERIFY=engine.run_verify_selected_job;ORIGINAL_SPLIT=engine.run_split_job
    engine.run_verify_selected_job=verify_selected;engine.run_verify_job=verify_file
    engine.Handler._sse=sse
    old_asset=engine.Handler._asset
    def asset(self,name):
        p=(engine.HERE/name).resolve()
        if p.is_file() and p.suffix in ('.html','.js','.css') and engine.HERE in p.parents:
            prefix=self.headers.get('X-Forwarded-Prefix','/ict/')
            if not re.fullmatch(r'/[A-Za-z0-9_/-]*/',prefix):prefix='/ict/'
            body=p.read_text().replace(BASE_MARKER,prefix).encode()
            return self._send(body,{'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}[p.suffix])
        return old_asset(self,name)
    engine.Handler._asset=asset
    old_get=engine.Handler.do_GET
    def worker_guard(self):
        secret=os.environ.get('UNDERBOX_ICT_WORKER_TOKEN','')
        if os.environ.get('UNDERBOX_GATEWAY_WORKER')=='1' and secret and not hmac.compare_digest(self.headers.get('X-Underbox-Worker','').encode(),secret.encode()):
            self._json({'error':'unauthorized_worker'},401);return True
        return False
    def get(self):
        if worker_guard(self):return
        if self.path.split('?',1)[0] in ('/health','/api/ict/status'):
            return self._json({'ok':True,'version':VERSION,'orchestration':'Huawei Cloud AgentArts','iam_configured':runtime_client.configured()})
        if self.path.split('?',1)[0]=='/api/ict/sample':
            data=json.loads(Path(os.environ['UNDERBOX_ICT_SAMPLE']).read_text())
            return self._json({'text':data.get('text') or '个人简历（脱敏摘录）\n个人信息\n姓名：'+data['candidate']+'\n身份背景：'+data['school']+'\n竞赛经历\n'+'\n'.join(c['raw_text'] for c in data['claims'])})
        return old_get(self)
    engine.Handler.do_GET=get
    old_post=engine.Handler.do_POST
    def post(self):
        if worker_guard(self):return
        match=re.fullmatch(r'/api/history/(\d+)/activate',self.path.split('?',1)[0])
        if match:
            self._read_json()
            return activate_history(self,int(match.group(1)))
        return old_post(self)
    engine.Handler.do_POST=post
    if os.environ.get('UNDERBOX_GATEWAY_WORKER')=='1':
        if not os.environ.get('UNDERBOX_ICT_BROKER_TOKEN'):raise RuntimeError('Missing isolated broker credential')
        broker=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('UNDERBOX_ICT_BROKER_PORT','18904'))),ToolHandler)
        threading.Thread(target=broker.serve_forever,daemon=True).start()
        if os.environ.get('UNDERBOX_ICT_EGRESS')=='public':
            import ict_egress
            ict_egress.install()

"""ICT edition integration. Original modules retain evidence and account behavior.

A single-use ticket authorizes one exact selected scope. The full report stays on
this server; AgentArts must invoke RunUnderbox and return its digest
receipt before the browser receives a completed report. No cloud bypass fallback.
"""
from __future__ import annotations
import contextvars,dataclasses,hashlib,hmac,http.client,json,os,queue,re,secrets,threading,time,urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import runtime_client

VERSION='ict-full-20261116.9'
USER=contextvars.ContextVar('ict_user',default='local')
LOCK=threading.RLock(); TICKETS={}; ACTIVE=threading.Lock()
ENGINE=None; ORIGINAL_VERIFY=None; ORIGINAL_SPLIT=None
BASE_MARKER='__ICT_BASE__'
TASK_TTL=3600

def scope_key(token,ids,materials):
    return hashlib.sha256(canonical([USER.get(),token,sorted(set(ids)),materials])).hexdigest()

def journal_path(key):
    db=os.environ.get('UNDERBOX_DB')
    if not db:return None
    directory=Path(db).parent/'ict-tasks'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    return directory/(key+'.json')

def persist_ticket(t):
    """Private crash recovery; never place resume/report content in process logs."""
    path=journal_path(t['scope'])
    if path is None:return
    data={k:t.get(k) for k in ('id','owner','scope','created_at','state','text','cloud_session','done','error','verified','unstarted')}
    if t.get('cost_budget'):data['cost_budget_id']=t['cost_budget'].id
    holder=t['holder'];rep=holder.get('oby');profile=holder.get('profile')
    if rep is not None:data['raw_report']=rep.model_dump(mode='json')
    if profile is not None:data['profile']=dataclasses.asdict(profile)
    temp=path.with_suffix('.'+secrets.token_hex(8)+'.tmp')
    try:
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(canonical(data));stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)

def load_ticket(key):
    path=journal_path(key)
    if path is None or not path.exists():return None
    data=json.loads(path.read_text())
    if data.get('scope')!=key or data.get('owner')!=USER.get():raise ValueError('核验任务不属于当前账户。')
    age=time.time()-data['created_at']
    if age>TASK_TTL:
        path.unlink();return None
    from app.schema import Report
    from app.strategy import ResumeProfile
    holder={}
    if data.get('raw_report'):holder['oby']=Report.model_validate(data['raw_report'])
    if data.get('profile'):holder['profile']=ResumeProfile(**data['profile'])
    if data['state']=='running':
        # A process restart cannot prove that an unfinished external operation is safe to repeat.
        data.update(state='interrupted',error='后台任务被服务重启中断，请检查历史记录；不会重新提交。')
    finished=threading.Event();finished.set()
    data.update(created=time.monotonic()-max(0,age),holder=holder,finished=finished,released=True,flow_lock=threading.Lock())
    if data.get('cost_budget_id'):
        from app.cost_budget import CostBudget
        data['cost_budget']=CostBudget.load(data['cost_budget_id'])
    if data['state']=='completed':data['receipt']=receipt_for(data)
    return data

def resumable_scope(owner,token,ids,materials):
    """Allow a completed private journal to survive loss of the in-memory split."""
    if not ids:return False
    ident=USER.set(owner)
    try:
        key=scope_key(token,ids,materials)
        with LOCK:
            t=next((t for t in TICKETS.values() if t['scope']==key and t['owner']==owner),None)
            if t is None:
                t=load_ticket(key)
                if t is not None:TICKETS[t['id']]=t
            return t is not None and t['state']!='awaiting' and not t.get('unstarted')
    finally:USER.reset(ident)

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
        if now-t['created']>TASK_TTL and t['state'] not in ('running',) and not t['flow_lock'].locked():
            TICKETS.pop(key,None)
            path=journal_path(t['scope'])
            if path:path.unlink(missing_ok=True)
    path=journal_path('_cleanup')
    if path:
        active={t['scope'] for t in TICKETS.values()}
        for saved in path.parent.glob('*.json'):
            if saved.stem not in active and time.time()-saved.stat().st_mtime>TASK_TTL:saved.unlink(missing_ok=True)

def register(token,ids,materials,events,holder):
    if not ids:raise ValueError('请至少选择1项。')
    key=scope_key(token,ids,materials)
    with LOCK:
        cleanup()
        existing=next((t for t in TICKETS.values() if t['scope']==key and t['owner']==USER.get()),None)
        if existing is None:
            existing=load_ticket(key)
            if existing is not None:TICKETS[existing['id']]=existing
        # Reuse a started/completed scope instead of paying for a second verification.
        if existing is not None:
            if existing['flow_lock'].locked():raise ValueError('本轮核验仍在处理中，请等待当前请求完成；不会重复提交。')
            if existing['state']=='awaiting' or existing.get('unstarted'):
                # Retire an unexecuted attempt. It must not shadow its successful replacement.
                existing['state']='failed';TICKETS.pop(existing['id'],None);release(existing)
            else:
                existing['flow_lock'].acquire()
                existing['events']=events
                holder.update(existing['holder'])
                return existing
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
        if not ACTIVE.acquire(blocking=False):raise ValueError('已有核验正在运行，请等待完成后再试。')
        rid='wb_'+secrets.token_urlsafe(24)
        flow_lock=threading.Lock();flow_lock.acquire()
        from app.cost_budget import CURRENT
        t={'id':rid,'owner':USER.get(),'scope':key,'created':time.monotonic(),'created_at':time.time(),'cost_budget':entry.get('cost_budget') or CURRENT.get(),'state':'awaiting','text':'UNDERBOX-WORKBENCH/1 '+rid+'\n'+text,
           'cloud_session':secrets.token_hex(16),'token':token,'ids':list(ids),'materials':materials,'events':events,'holder':holder,
           'context':contextvars.copy_context(),'finished':threading.Event(),'done':None,'error':None,'released':False,'flow_lock':flow_lock}
        from app.usage import METER
        scope=METER.mark()['_scope'];t['meter_scope']=scope if METER.retain_scope(scope) else None
        TICKETS[rid]=t
        try:persist_ticket(t)
        except Exception:
            TICKETS.pop(rid,None);release(t);flow_lock.release();raise
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
    finally:
        try:persist_ticket(t)
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
                try:persist_ticket(t)
                except Exception:
                    t['state']='awaiting';raise
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
        path=journal_path(t['scope'])
        if path:path.unlink(missing_ok=True)
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
    if t['state'] in ('failed','interrupted'):raise RuntimeError('backend_verification_failed')
    # Preserve the deployed tool's existing three-field submit contract.
    # The opaque first line is transport metadata, not a new resume claim.
    payload={'action':'submit','resume_text':t['text'],'consent':True}
    query=('我已获得本次材料处理授权，并同意由华为云AgentArts、服务器DeepSeek及智谱搜索处理以下选中事项。'
           '请调用RunUnderbox提交以下JSON一次，不要修改resume_text（第一行是工作台传输标识，不是经历）。'
           '拿到job_id后用action=status查询至completed，最多20次，不重复submit。完成后直接返回工具实际result_json，失败如实说明。\n'+json.dumps(payload,ensure_ascii=False))
    status_query=('继续已提交任务，仅实际调用RunUnderbox查询以下JSON的status。禁止submit、重新核验、搜索或处理新材料。'
                  '完成后直接返回工具实际result_json，不要自行生成回执。\n'+json.dumps({'action':'status','job_id':t['id']}))
    started=time.monotonic();deadline=started+900;count=0;types=[]
    if t.get('verified'):
        digest=hashlib.sha256(canonical(t['done']['report'])).hexdigest()
        if t['verified'].get('receipt_sha256')!=digest:raise RuntimeError('platform_receipt_missing_or_changed')
        return t['verified']
    if t['state']!='awaiting':query=status_query
    for attempt in range(4):
        if time.monotonic()>=deadline:raise TimeoutError('platform_timeout')
        outputs=[];text=''
        try:
            with runtime_client.invoke(query,t['cloud_session'],timeout=min(480,max(1,deadline-time.monotonic()))) as response:
                for raw in response:
                    if time.monotonic()>deadline:raise TimeoutError('platform_timeout')
                    if not raw.startswith(b'data:'):continue
                    try:event=json.loads(raw[5:])
                    except ValueError:continue
                    if not isinstance(event,dict):continue
                    count+=1;types.append(str(event.get('event','')))
                    if event.get('event') in ('error','failed'):raise RuntimeError('platform_failed')
                    # Only inspect public outputs and real tool results, never model reasoning or echoed inputs.
                    event={k:v for k,v in event.items() if k!='reasoning_content'}
                    outputs.append(event)
                    content=event.get('content')
                    if event.get('event')=='message' and isinstance(content,str):text+=content
                    elif event.get('event')=='summary_response' and isinstance(content,str):text=content
                    if len(text)>1024*1024 or count>30000:raise ValueError('platform_response_too_large')
        except (OSError,http.client.HTTPException,RuntimeError) as exc:
            if t['state']=='awaiting' or str(exc)=='iam_not_configured':raise
            # After a tool has started, recover only by status, including transient cloud disconnects.
        t['platform_event_types']=list(dict.fromkeys(types))
        if t['state']=='awaiting':raise RuntimeError('platform_did_not_start_tool')
        if t['state']=='running':
            t['events'].put({'log':'云端本轮响应已结束，后台仍在核验；完成后将只查询结果，不会重复提交。'})
            t['finished'].wait(max(0,deadline-time.monotonic()))
        if t['state'] in ('failed','interrupted'):raise RuntimeError('backend_verification_failed')
        done=t.get('done') or {}
        if t['state']=='completed' and done.get('report'):
            digest=hashlib.sha256(canonical(done['report'])).hexdigest()
            if find_receipt([outputs,text],t['id'],digest):
                info={'session':t['cloud_session'],'request':t['id'],'events':count,'receipt_sha256':digest,
                      'elapsed_seconds':round(time.monotonic()-started,2),'status_recoveries':attempt}
                t['verified']=info;persist_ticket(t)
                return info
        if time.monotonic()>=deadline:raise TimeoutError('platform_timeout')
        query=status_query
    raise RuntimeError('platform_receipt_missing_or_changed')

def verify_selected(token,ids,events,holder,*,materials=None):
    started=time.monotonic();ticket=None
    try:
        ticket=register(token,ids,materials,events,holder)
        if ticket.get('verified'):message='本轮报告已有匹配回执，正在恢复已保存结果；不重复核验或调用云端。'
        elif ticket['state'] in ('running','completed'):message='正在通过 AgentArts 查询已保留的本轮任务，不重新提交简历。'
        else:message='已确认核验范围，正在请求华为云 AgentArts 编排。'
        events.put({'log':message})
        from app.cost_budget import bind
        with bind(ticket.get('cost_budget')):info=cloud_roundtrip(ticket)
        holder.update(ticket['holder'])
        event=dict(ticket['done']);event['seconds']=round(time.monotonic()-started,1);event['_ict_request']=ticket['id']
        event['report']={**event['report'],'agentarts':info,'edition':'huawei-ict'}
        if ticket.get('cost_budget'):
            event['costBudget']=ticket['cost_budget'].snapshot()
            event['report']['costBudget']=event['costBudget']
        events.put({'log':'AgentArts 已返回与本轮完整报告匹配的回执。'})
        events.put(event)
    except Exception as exc:
        if ticket and ticket['state']=='awaiting':
            ticket.update(state='failed',unstarted=True);release(ticket);persist_ticket(ticket)
        codes={'iam_not_configured':'IAM配置缺失','platform_failed':'平台执行错误','platform_did_not_start_tool':'平台未启动工作台工具','platform_receipt_missing_or_changed':'平台回执缺失或不匹配'}
        code=codes.get(str(exc),type(exc).__name__)
        state=ticket['state'] if ticket else None
        if state=='completed':message='核验已完成，云端回执暂未确认。结果已保留，可重试获取回执；不会重新核验。'
        elif state=='running':message='后台仍在核验，任务已保留。稍后重试将继续获取本轮结果，不会重新提交。'
        elif str(exc)=='backend_verification_failed':message=ticket.get('error') or '后台核验未完成，请检查服务状态。'
        else:message=str(exc) if isinstance(exc,ValueError) else 'AgentArts 调用未完成（'+code+'），未自动重复提交。'
        events.put({'done':True,'failed':True,'error':message,'seconds':round(time.monotonic()-started,1)})
    finally:
        if ticket:ticket['flow_lock'].release()
        events.put(None)

def record_verified_ticket(request_id,ev,uid,holder):
    """Commit history, QA context, usage and quota once, including resumed requests."""
    import store
    t=TICKETS.get(request_id)
    if not t or t['owner']!=uid or not t.get('verified'):raise ValueError('unconfirmed_ticket')
    report=ev['report']
    if report.get('agentarts')!=t['verified']:raise ValueError('receipt_changed')
    marker='ict-task-final:'+request_id
    with store.connect(os.environ['UNDERBOX_DB']) as conn:
        conn.execute('BEGIN IMMEDIATE');context_table(conn)
        saved=conn.execute('SELECT value FROM meta WHERE key=?',(marker,)).fetchone()
        if saved:return int(saved['value'])
        store.record_usage(conn,uid,kind='verify',claims=len(report.get('claims') or []),seconds=ev.get('seconds',0),usage=ev.get('usage'),ok=True)
        store.consume_quota(conn,uid)
        report_id=store.save_report(conn,uid,report)
        rep=holder['oby'];profile=holder.get('profile')
        conn.execute('INSERT INTO ict_report_context VALUES(?,?,?)',(report_id,json.dumps(rep.model_dump(mode='json'),ensure_ascii=False),json.dumps(dataclasses.asdict(profile) if profile is not None else {},ensure_ascii=False)))
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?)',(marker,str(report_id)))
        conn.commit()
    return report_id

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
    from app.cost_budget import CURRENT,CostBudget,bind as bind_cost
    from app.retrieval_policy import enabled as recall_enabled
    ledger=None
    scope=METER.open_scope(owners=2);base=METER.mark(scope)
    unsubscribe=METER.subscribe(lambda:events.put({'usage':METER.since(base),**({'costBudget':ledger.snapshot()} if ledger else {})}),scope)
    class Tracking:
        completed=False
        def put(q,ev):
            if ev and ev.get('done') and not q.completed:
                q.completed=True
                if kind=='split' and ev.get('split',{}).get('token'):
                    with ENGINE._PREPARED_LOCK:
                        entry=ENGINE.PREPARED.get(ev['split']['token'])
                        if entry is not None:
                            entry['owner']=uid
                            if ledger:entry['cost_budget']=ledger
                if ledger:ev.setdefault('costBudget',ledger.snapshot())
                ev['usage']=METER.since(base);request_id=ev.pop('_ict_request',None)
                if request_id and kind=='verify' and ev.get('report'):
                    try:record_verified_ticket(request_id,ev,uid,holder)
                    except Exception:
                        # Keep the private ticket for a safe retry; never report an unsaved success.
                        ev={'done':True,'failed':True,'error':'核验结果已保留，但历史记录保存未完成。请重试获取本轮结果，不会重新核验。','usage':ev['usage']}
                        ENGINE.record_gateway_usage(kind,ev,uid)
                else:
                    saved_id=ENGINE.record_gateway_usage(kind,ev,uid)
                    if saved_id and holder.get('oby') is not None:save_context(saved_id,holder)
                if 'report' in ev:
                    with ENGINE._STATE_LOCK:
                        state=ENGINE._STATES.get(uid)
                        if state and state['generation']==generation:state.update(presentation=ev['report'],oby=holder.get('oby'),profile=holder.get('profile'))
            events.put(ev)
    tracker=Tracking()
    def run():
        nonlocal ledger
        ident=USER.set(uid)
        try:
            if recall_enabled() and kind=='split':ledger=CostBudget()
            elif recall_enabled() and kind=='verify':
                if args and isinstance(args[0],str):
                    with ENGINE._PREPARED_LOCK:
                        entry=ENGINE.PREPARED.get(args[0])
                        if entry is not None and entry.get('owner')==uid:
                            ledger=entry.get('cost_budget') or CostBudget();entry['cost_budget']=ledger
                else:ledger=CostBudget()
            if ledger:events.put({'costBudget':ledger.snapshot()})
            with bind_search_credentials(credentials),bind_cost(ledger):METER.run_in_scope(scope,target,*args,tracker,holder)
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
            from app.retrieval_policy import enabled
            from app.cost_budget import enforced
            return self._json({'ok':True,'version':VERSION,'orchestration':'Huawei Cloud AgentArts','iam_configured':runtime_client.configured(),
                               'retrieval':{'expanded':enabled(),'search_ceiling':100,'read_ceiling':40,
                                            'failed_read_recovery_ceiling':8,'alternate_searches_per_claim':0 if os.environ.get('UNDERBOX_ICT_GLM_ONLY')=='1' else 4,
                                            'search_policy':'zhipu_only' if os.environ.get('UNDERBOX_ICT_GLM_ONLY')=='1' else 'configured_with_fallback',
                                            'cost_budget_enforced':enforced(),
                                            'estimated_budget_cny':4 if enforced() else None,
                                            'target_cny':3 if enforced() else None,'cloud_allowance_cny':.5,
                                            'report_search_safety_limit':1000}})
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

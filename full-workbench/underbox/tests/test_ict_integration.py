"""Behavioral migration checks. Temporary accounts, fake providers, loopback only."""
import contextlib,http.client,io,json,os,queue,shutil,socket,sys,tempfile,threading,time,unittest,urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'underbox'),str(ROOT/'open_box')]
os.environ.update(UNDERBOX_ICT_ENABLE='1',HUAWEICLOUD_SDK_AK='synthetic-ak',HUAWEICLOUD_SDK_SK='synthetic-sk')
import serve,accounts,store,ict_extension as ict
from app.schema import Report,Claim,VerifiedClaim
from app.strategy import ResumeProfile
from app.usage import METER
REPORT=Report.model_validate(json.loads((ROOT/'open_box/fixtures/report_lin.json').read_text()))
REPORT.claims=REPORT.claims[:1];REPORT.report_no='SYNTHETIC-ICT';REPORT.candidate_name='合成测试甲'
PROFILE=ResumeProfile(identity='student',name='合成测试甲')

def fake_core(token,ids,events,holder,*,materials=None):
    holder.update(oby=REPORT.model_copy(deep=True),profile=PROFILE)
    METER.record_llm('deepseek',{'prompt_tokens':11,'completion_tokens':7})
    events.put({'log':'合成离线核验事件'})
    events.put({'done':True,'report':serve.to_presentation(holder['oby']),'seconds':.1,'filename':'synthetic.txt','fileType':'txt'})
    events.put(None)

class Stream(io.BytesIO):status=200

def cloud_ok(query,session_id,**kwargs):
    data=json.loads(query.split('\n',1)[1]);assert session_id
    if data['action']=='submit':
      assert set(data)=={'action','resume_text','consent'}
      request_id=data['resume_text'].splitlines()[0].split(' ',1)[1]
      ict.tool_action(data);result=ict.tool_action({'action':'status','job_id':request_id})
    else:
      assert set(data)=={'action','job_id'} and data['action']=='status'
      result=ict.tool_action(data)
    return Stream(('data: '+json.dumps({'event':'summary_response','content':result['result_json']})+'\n\ndata: {"event":"done"}\n').encode())

class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
    cls.tmp=tempfile.TemporaryDirectory(prefix='ict-offline-');cls.db=Path(cls.tmp.name)/'users.db';store.init(cls.db)
    os.environ.update(UNDERBOX_DB=str(cls.db),UNDERBOX_GATEWAY_WORKER='1',UNDERBOX_ICT_WORKER_TOKEN='synthetic-worker-secret')
    with store.connect(cls.db) as c:
      for uid,role,quota in [('alice','user',5),('bob','user',0),('admin','admin',-1)]:store.create_user(c,uid,'合成'+uid,'test-password',role=role,quota_total=quota)
      c.commit()
    cls.worker=serve.ThreadingHTTPServer(('127.0.0.1',0),serve.Handler);threading.Thread(target=cls.worker.serve_forever,daemon=True).start()
    cls.acc=accounts.Accounts(cls.db)
    cls.gateway=serve.ThreadingHTTPServer(('127.0.0.1',0),accounts.account_handler(serve.Handler,cls.acc,cls.worker.server_port));threading.Thread(target=cls.gateway.serve_forever,daemon=True).start()
    cls.broker=serve.ThreadingHTTPServer(('127.0.0.1',0),ict.ToolHandler);threading.Thread(target=cls.broker.serve_forever,daemon=True).start()
 @classmethod
 def tearDownClass(cls):
    for h in [cls.gateway,cls.worker,cls.broker]:h.shutdown();h.server_close()
    cls.tmp.cleanup()
 def setUp(self):
    with store.connect(self.db) as c:
      for table in ['ict_report_context','report_history','usage_log','quota_requests']:
        try:c.execute('DELETE FROM '+table)
        except Exception:pass
      c.execute('UPDATE users SET quota_used=0');c.commit()
    serve._STATES.clear();serve.PREPARED.clear();self.acc.attempts.clear()
    for t in list(ict.TICKETS.values()):ict.release(t)
    ict.TICKETS.clear()
    shutil.rmtree(self.db.parent/'ict-tasks',ignore_errors=True)
 def request(self,path,body=None,cookie='',extra=None,method=None,worker=False):
    h={'Host':'127.0.0.1:'+str(self.gateway.server_port),'X-Forwarded-Proto':'https','X-Forwarded-Prefix':'/ict-test/'}
    if cookie:h['Cookie']=cookie
    if body is not None:h['Content-Type']='application/json'
    h.update(extra or {});con=http.client.HTTPConnection('127.0.0.1',(self.worker if worker else self.gateway).server_port,timeout=8)
    raw=json.dumps(body) if body is not None and not isinstance(body,(bytes,str)) else body
    con.request(method or ('POST' if body is not None else 'GET'),path,raw,h);r=con.getresponse();data=r.read();result=r.status,dict(r.getheaders()),data;con.close();return result
 def login(self,uid='alice'):
    code,h,b=self.request('/api/login',{'username':uid,'password':'test-password'});self.assertEqual(code,200);return h['Set-Cookie'].split(';')[0]
 def prepared(self,owner='alice'):
    token='prepared-'+owner
    serve.PREPARED[token]={'owner':owner,'prepared':SimpleNamespace(name='合成测试甲',claims=[REPORT.claims[0].claim],skipped=[],profile=PROFILE),'filename':'synthetic.txt','fileType':'txt'}
    return token
 def test_login_cookie_scoped_secure(self):
    code,h,b=self.request('/api/login',{'username':'alice','password':'test-password'});self.assertEqual(code,200)
    for flag in ['Path=/ict-test/','Secure','HttpOnly','SameSite=Strict']:self.assertIn(flag,h['Set-Cookie'])
 def test_worker_rejects_direct_identity_spoofing(self):
    self.assertEqual(self.request('/api/report',extra={'X-Underbox-User':'alice'},worker=True)[0],401)
 def test_egress_rejects_private_metadata_and_arbitrary_ports(self):
    from ict_egress import allowed_ip
    for host,port in [('169.254.169.254',80),('10.0.0.1',443),('127.0.0.1',18906),('8.8.8.8',22),('::ffff:127.0.0.1',80)]:self.assertFalse(allowed_ip(host,port))
    self.assertTrue(allowed_ip('8.8.8.8',443));self.assertTrue(allowed_ip('127.0.0.1',8080))
 def test_invalid_password(self):self.assertEqual(self.request('/api/login',{'username':'alice','password':'wrong'})[0],401)
 def test_logout_revokes_session(self):
    c=self.login();self.assertEqual(self.request('/api/logout',{},c)[0],200);self.assertFalse(json.loads(self.request('/api/session',cookie=c)[2])['authed'])
 def test_cross_origin_blocked(self):self.assertEqual(self.request('/api/login',{'username':'alice','password':'test-password'},extra={'Origin':'https://evil.invalid'})[0],403)
 def test_anonymous_private_api_blocked(self):self.assertEqual(self.request('/api/history')[0],401)
 def test_static_sources_unavailable(self):
    for path in ['/serve.py','/store.py','/ict_extension.py','/.env','/../users.db']:
      self.assertEqual(self.request(path)[0],404)
 def test_assets_use_proxy_prefix(self):
    data=self.request('/')[2].decode();self.assertIn('/ict-test/api/session',data);self.assertNotIn('__ICT_BASE__',data)
 def test_admin_denied_for_member(self):self.assertEqual(self.request('/api/admin/users',cookie=self.login())[0],403)
 def test_admin_users_and_overview(self):
    c=self.login('admin');self.assertEqual(self.request('/api/admin/users',cookie=c)[0],200);self.assertEqual(self.request('/api/admin/overview',cookie=c)[0],200)
 def test_quota_gate_before_call(self):
    c=self.login('bob');self.assertEqual(self.request('/api/verify/stream',{'token':'x','claimIds':['c1']},c,{'X-Underbox-Consent':'1'})[0],402)
 def test_processing_requires_explicit_consent(self):self.assertEqual(self.request('/api/split/stream',{},self.login())[0],403)
 def test_identity_header_cannot_switch_account(self):self.assertEqual(self.request('/api/history',cookie=self.login(),extra={'X-Underbox-User':'bob'})[0],409)
 def test_latest_ten_history_and_account_isolation(self):
    with store.connect(self.db) as c:
      for n in range(12):store.save_report(c,'alice',{'reportNo':str(n),'candidate':'测试','claims':[]})
      other=store.save_report(c,'bob',{'reportNo':'private','candidate':'另一个合成人','claims':[]});c.commit()
    ca=self.login();items=json.loads(self.request('/api/history',cookie=ca)[2])['items'];self.assertEqual(len(items),10);self.assertEqual(items[0]['reportNo'],'11');self.assertEqual(items[-1]['reportNo'],'2')
    self.assertEqual(self.request('/api/history/'+str(other),cookie=ca)[0],404);self.assertEqual(self.request('/api/history/'+str(other)+'/export',cookie=ca)[0],404)
 def test_json_and_markdown_export(self):
    serve.user_state('alice')['presentation']=serve.to_presentation(REPORT);c=self.login()
    j=self.request('/api/export?format=json',cookie=c);m=self.request('/api/export?format=md',cookie=c)
    self.assertEqual(j[0],200);self.assertEqual(json.loads(j[2])['candidate'],'合成测试甲');self.assertIn('合成测试甲',m[2].decode());self.assertIn('陈述原文',m[2].decode())
 def test_query_reset_keeps_login(self):
    serve.user_state('alice')['presentation']={'claims':[]};c=self.login();self.assertEqual(self.request('/api/query/reset',{},c)[0],200);self.assertTrue(json.loads(self.request('/api/session',cookie=c)[2])['authed']);self.assertIsNone(serve.user_state('alice')['presentation'])
 def test_ticket_owner_guard(self):
    token=self.prepared('alice');u=ict.USER.set('bob')
    try:self.assertRaises(ValueError,ict.register,token,[REPORT.claims[0].claim.id],{},queue.Queue(),{})
    finally:ict.USER.reset(u)
 def test_ticket_requires_exact_selected_scope(self):
    u=ict.USER.set('alice')
    try:
      t=ict.register(self.prepared(),[REPORT.claims[0].claim.id],{},queue.Queue(),{})
      self.assertRaises(ValueError,ict.tool_action,{'action':'submit','workbench_request':t['id'],'consent':True,'resume_text':'changed'})
      self.assertEqual(t['state'],'awaiting')
    finally:ict.USER.reset(u)
 def test_broker_requires_secret(self):
    con=http.client.HTTPConnection('127.0.0.1',self.broker.server_port);con.request('POST','/tool',b'{}',{'Content-Type':'application/json'});r=con.getresponse();r.read();self.assertEqual(r.status,401);con.close()
 def test_forged_receipt_rejected(self):
    self.assertFalse(ict.find_receipt({'workbench_receipt':{'request_id':'x','report_sha256':'wrong'}},'x','expected'))
 def test_report_receipt_cannot_come_from_input(self):
    ref={'workbench_receipt':{'request_id':'x','report_sha256':'yes'}};self.assertFalse(ict.find_receipt({'query':ref},'x','yes'))
 def test_verified_cloud_flow_saves_history_and_quota_once(self):
    token=self.prepared();c=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      code,h,raw=self.request('/api/verify/stream',{'token':token,'claimIds':[REPORT.claims[0].claim.id]},c,{'X-Underbox-Consent':'1'})
    events=[json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ')];done=next(e for e in events if e.get('done'))
    self.assertEqual(code,200);self.assertNotIn('failed',done);self.assertIn('agentarts',done['report']);self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1)
    self.assertEqual(done['usage']['deepseek']['calls'],1)
    with store.connect(self.db) as db:
      self.assertEqual(store.get_user(db,'alice')['quota_used'],1);self.assertEqual(len(store.list_reports(db,'alice')),1);self.assertEqual(db.execute('SELECT COUNT(*) FROM ict_report_context').fetchone()[0],1)
 def test_fourteen_selected_items_share_budget_and_resume_without_rerun(self):
    from app.cost_budget import CURRENT
    many=REPORT.model_copy(deep=True)
    many.claims=[VerifiedClaim(claim=Claim(id='synthetic-'+str(i),category='项目',raw_text='虚构项目事项'+str(i),raw_locator='合成测试',date_label='',elements=[],entities={}),status='none') for i in range(14)]
    ids=[v.claim.id for v in many.claims];cookie=self.login()
    def charged(*args,**kwargs):
      CURRENT.get().reserve(.2,'synthetic_verify');return fake_core(*args,**kwargs)
    def request():
      code,_,raw=self.request('/api/verify/stream',{'token':token,'claimIds':ids},cookie,{'X-Underbox-Consent':'1'})
      self.assertEqual(code,200)
      return next(json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ') and json.loads(line[6:]).get('done'))
    with patch(__name__+'.REPORT',many),patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1'}),patch.object(ict,'ORIGINAL_VERIFY',side_effect=charged) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      token=self.prepared();serve.PREPARED[token]['prepared'].claims=[v.claim for v in many.claims]
      first=request();second=request();ict.TICKETS.clear();serve.PREPARED.clear();restored=request()
    self.assertFalse(first.get('failed'));self.assertEqual([c['id'] for c in first['report']['claims']],ids)
    self.assertEqual(first['report']['costBudget'],second['report']['costBudget']);self.assertEqual(first['report']['costBudget'],restored['report']['costBudget'])
    self.assertAlmostEqual(first['report']['costBudget']['estimatedCny'],.7)
    self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1)
    with store.connect(self.db) as db:
      self.assertEqual(store.get_user(db,'alice')['quota_used'],1);self.assertEqual(len(store.list_reports(db,'alice')),1)
      self.assertEqual(db.execute('SELECT COUNT(*) FROM ict_report_context').fetchone()[0],1)
 def test_disabled_money_limit_keeps_cloud_receipt_history_and_quota(self):
    from app.cost_budget import CURRENT
    cookie=self.login();token=self.prepared()
    def charged(*args,**kwargs):
      CURRENT.get().reserve(5,'synthetic_verify');return fake_core(*args,**kwargs)
    with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1','UNDERBOX_ICT_COST_BUDGET':'0'}),patch.object(ict,'ORIGINAL_VERIFY',side_effect=charged) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      code,_,raw=self.request('/api/verify/stream',{'token':token,'claimIds':[REPORT.claims[0].claim.id]},cookie,{'X-Underbox-Consent':'1'})
      done=next(json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ') and json.loads(line[6:]).get('done'))
      _,_,status_raw=self.request('/api/ict/status',cookie=cookie)
    self.assertEqual(code,200);self.assertFalse(done.get('failed'));self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1)
    self.assertFalse(done['report']['costBudget']['enforced']);self.assertIsNone(done['report']['costBudget']['limitCny'])
    self.assertAlmostEqual(done['report']['costBudget']['estimatedCny'],5.5)
    status=json.loads(status_raw)['retrieval'];self.assertFalse(status['cost_budget_enforced']);self.assertIsNone(status['estimated_budget_cny'])
    with store.connect(self.db) as db:
      self.assertEqual(store.get_user(db,'alice')['quota_used'],1);self.assertEqual(len(store.list_reports(db,'alice')),1)
      self.assertEqual(db.execute('SELECT COUNT(*) FROM ict_report_context').fetchone()[0],1)
 def test_unlimited_count_still_rejects_empty_and_invalid_selected_ids(self):
    u=ict.USER.set('alice')
    try:
      token=self.prepared()
      self.assertRaises(ValueError,ict.register,token,[],{},queue.Queue(),{})
      self.assertFalse(ict.resumable_scope('alice',token,[],{}))
      self.assertRaises(ValueError,ict.register,token,[REPORT.claims[0].claim.id]+['forged-'+str(i) for i in range(14)],{},queue.Queue(),{})
      self.assertFalse(ict.TICKETS)
    finally:ict.USER.reset(u)
 def test_unlimited_count_preserves_cloud_text_size_guard(self):
    token=self.prepared();base=REPORT.claims[0].claim
    items=[base.model_copy(update={'id':'synthetic-'+str(i),'raw_text':'虚构材料'*1100}) for i in range(9)]
    serve.PREPARED[token]['prepared'].claims=items;u=ict.USER.set('alice')
    try:self.assertRaisesRegex(ValueError,'32768',ict.register,token,[c.id for c in items],{},queue.Queue(),{})
    finally:ict.USER.reset(u)
 def test_split_verify_retry_and_restart_share_one_cost_budget(self):
    from app.cost_budget import CURRENT
    cookie=self.login();seen=[]
    def split(data,filename,events,holder):
      ledger=CURRENT.get();ledger.reserve(.1,'synthetic_parse');seen.append(ledger.id)
      token=self.prepared();events.put({'done':True,'split':{'token':token,'claims':[]},'seconds':.1});events.put(None)
    def core(*args,**kwargs):
      ledger=CURRENT.get();self.assertEqual(ledger.id,seen[0]);ledger.reserve(.2,'synthetic_verify');return fake_core(*args,**kwargs)
    with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1'}),patch.object(serve,'run_split_job',side_effect=split):
      code,_,raw=self.request('/api/split/stream',b'synthetic',cookie,{'X-Underbox-Consent':'1','Content-Type':'text/plain','X-Filename':'synthetic.txt'})
    self.assertEqual(code,200)
    with patch.dict(os.environ,{'UNDERBOX_ICT_RECALL':'1'}),patch.object(ict,'ORIGINAL_VERIFY',side_effect=core) as verify,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      done=self.verify_request('prepared-alice',cookie);again=self.verify_request('prepared-alice',cookie)
      ict.TICKETS.clear();serve.PREPARED.clear();reloaded=self.verify_request('prepared-alice',cookie)
    self.assertEqual(verify.call_count,1);self.assertEqual(cloud.call_count,1)
    for event in (done,again,reloaded):self.assertAlmostEqual(event['report']['costBudget']['estimatedCny'],.8)
    with store.connect(self.db) as conn:self.assertEqual(store.get_user(conn,'alice')['quota_used'],1)
 def verify_request(self,token,cookie):
    code,h,raw=self.request('/api/verify/stream',{'token':token,'claimIds':[REPORT.claims[0].claim.id]},cookie,{'X-Underbox-Consent':'1'})
    self.assertEqual(code,200)
    return next(json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ') and json.loads(line[6:]).get('done'))
 def test_long_job_stream_end_recovers_by_status_only(self):
    token=self.prepared();cookie=self.login();actions=[];sessions=[]
    def slow_core(*args,**kwargs):time.sleep(.15);return fake_core(*args,**kwargs)
    def early_end(query,session,**kwargs):
      data=json.loads(query.split('\n',1)[1]);actions.append(data['action']);sessions.append(session)
      if len(actions)==1:
        ict.tool_action(data)
        return Stream(b'data: {"event":"done"}\n')
      return cloud_ok(query,session)
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=slow_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=early_end):
      done=self.verify_request(token,cookie)
    self.assertNotIn('failed',done);self.assertEqual(actions,['submit','status']);self.assertEqual(len(set(sessions)),1);self.assertEqual(core.call_count,1)
    self.assertEqual(done['report']['agentarts']['status_recoveries'],1);self.assertNotIn('_ict_request',done)
 def test_disconnect_after_submit_recovers_without_new_submit(self):
    token=self.prepared();cookie=self.login();actions=[]
    def disconnect(query,session,**kwargs):
      data=json.loads(query.split('\n',1)[1]);actions.append(data['action'])
      if len(actions)==1:ict.tool_action(data);raise urllib.error.URLError('synthetic_disconnect')
      return cloud_ok(query,session)
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=disconnect):
      done=self.verify_request(token,cookie)
    self.assertNotIn('failed',done);self.assertEqual(actions,['submit','status']);self.assertEqual(core.call_count,1)
 def missing_receipt(self,query,session,**kwargs):
    data=json.loads(query.split('\n',1)[1]);result=ict.tool_action(data)
    if data['action']=='submit':ict.tool_action({'action':'status','job_id':result['job_id']})
    return Stream(b'data: {"event":"summary_response","content":"SYNTHETIC_PRIVATE_MODEL_NARRATIVE"}\n')
 def test_pending_receipt_retry_reuses_report_and_qa_context(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core:
      with patch.object(ict.runtime_client,'invoke',side_effect=self.missing_receipt) as cloud:first=self.verify_request(token,cookie)
      self.assertTrue(first['failed']);self.assertIn('核验已完成',first['error']);self.assertNotIn('SYNTHETIC_PRIVATE_MODEL_NARRATIVE',json.dumps(first));self.assertEqual(cloud.call_count,4)
      with store.connect(self.db) as c:self.assertEqual(store.get_user(c,'alice')['quota_used'],0);self.assertEqual(store.list_reports(c,'alice'),[])
      with patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:done=self.verify_request(token,cookie)
      self.assertNotIn('failed',done);self.assertEqual(core.call_count,1);self.assertEqual(json.loads(cloud.call_args.args[0].split('\n',1)[1])['action'],'status')
    with store.connect(self.db) as c:
      self.assertEqual(store.get_user(c,'alice')['quota_used'],1);self.assertEqual(len(store.list_reports(c,'alice')),1);self.assertEqual(c.execute('SELECT COUNT(*) FROM ict_report_context').fetchone()[0],1)
      self.assertEqual(c.execute('SELECT SUM(ds_calls) FROM usage_log').fetchone()[0],1)
 def test_repeated_success_does_not_repeat_cloud_core_history_or_quota(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      first=self.verify_request(token,cookie);second=self.verify_request(token,cookie)
    self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1);self.assertEqual(first['report'],second['report'])
    with store.connect(self.db) as c:
      self.assertEqual(len(store.list_reports(c,'alice')),1);self.assertEqual(store.get_user(c,'alice')['quota_used'],1);self.assertEqual(c.execute('SELECT COUNT(*) FROM usage_log WHERE ok=1').fetchone()[0],1)
 def test_unstarted_cloud_failure_cannot_shadow_successful_replacement(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=[urllib.error.HTTPError('https://example.invalid',503,'synthetic_unavailable',{},None)]):
      first=self.verify_request(token,cookie)
    self.assertTrue(first['failed']);core.assert_not_called()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      second=self.verify_request(token,cookie);third=self.verify_request(token,cookie)
    self.assertNotIn('failed',second);self.assertEqual(second['report'],third['report']);self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1);self.assertEqual(len(ict.TICKETS),1)
    with store.connect(self.db) as c:self.assertEqual(len(store.list_reports(c,'alice')),1);self.assertEqual(store.get_user(c,'alice')['quota_used'],1)
 def test_private_journal_recovers_completed_task_after_worker_restart(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core:
      with patch.object(ict.runtime_client,'invoke',side_effect=self.missing_receipt):self.assertTrue(self.verify_request(token,cookie)['failed'])
      files=list((self.db.parent/'ict-tasks').glob('*.json'));self.assertEqual(len(files),1);self.assertEqual(files[0].stat().st_mode&0o777,0o600)
      self.assertEqual(files[0].parent.stat().st_mode&0o777,0o700)
      ict.TICKETS.clear();serve.PREPARED.clear()
      with patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok):done=self.verify_request(token,cookie)
    self.assertNotIn('failed',done);self.assertEqual(core.call_count,1)
    with store.connect(self.db) as c:rid=store.list_reports(c,'alice')[0]['id']
    self.assertEqual(self.request('/api/history/'+str(rid)+'/activate',{},cookie)[0],200);self.assertEqual(serve.user_state('alice')['oby'].candidate_name,'合成测试甲')
 def test_verified_journal_replay_is_idempotent_after_restart(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      first=self.verify_request(token,cookie);ict.TICKETS.clear();serve.PREPARED.clear();second=self.verify_request(token,cookie)
    self.assertNotIn('failed',second);self.assertEqual(first['report'],second['report']);self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1)
    with store.connect(self.db) as c:self.assertEqual(len(store.list_reports(c,'alice')),1);self.assertEqual(store.get_user(c,'alice')['quota_used'],1)
 def test_journal_cannot_be_replayed_under_another_owner(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core),patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok):self.verify_request(token,cookie)
    u=ict.USER.set('bob')
    try:self.assertRaises(ValueError,ict.register,token,[REPORT.claims[0].claim.id],{},queue.Queue(),{})
    finally:ict.USER.reset(u)
 def test_backend_failure_retains_error_without_retry_or_quota(self):
    token=self.prepared();cookie=self.login()
    def fail_core(token,ids,events,holder,**kwargs):events.put({'done':True,'failed':True,'error':'合成检索不可用'});events.put(None)
    def fail_cloud(query,session,**kwargs):
      data=json.loads(query.split('\n',1)[1]);result=ict.tool_action(data);ict.tool_action({'action':'status','job_id':result['job_id']});return Stream(b'data: {"event":"done"}\n')
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fail_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=fail_cloud) as cloud:
      first=self.verify_request(token,cookie);second=self.verify_request(token,cookie)
    self.assertIn('合成检索不可用',first['error']);self.assertEqual(second['error'],first['error']);self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1)
    with store.connect(self.db) as c:self.assertEqual(store.get_user(c,'alice')['quota_used'],0)
 def test_running_journal_after_restart_never_resubmits(self):
    u=ict.USER.set('alice')
    try:
      t=ict.register(self.prepared(),[REPORT.claims[0].claim.id],{},queue.Queue(),{});t['state']='running';ict.persist_ticket(t);ict.release(t);t['flow_lock'].release();ict.TICKETS.clear();serve.PREPARED.clear()
    finally:ict.USER.reset(u)
    with patch.object(ict,'ORIGINAL_VERIFY') as core,patch.object(ict.runtime_client,'invoke') as cloud:done=self.verify_request('prepared-alice',self.login())
    self.assertTrue(done['failed']);self.assertIn('服务重启',done['error']);core.assert_not_called();cloud.assert_not_called()
 def test_history_write_failure_rolls_back_and_retry_uses_existing_result(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core) as core,patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok) as cloud:
      with patch.object(store,'save_report',side_effect=RuntimeError('synthetic_database_error')):first=self.verify_request(token,cookie)
      self.assertTrue(first['failed']);self.assertIn('已保留',first['error'])
      with store.connect(self.db) as c:self.assertEqual(len(store.list_reports(c,'alice')),0);self.assertEqual(store.get_user(c,'alice')['quota_used'],0)
      done=self.verify_request(token,cookie)
    self.assertNotIn('failed',done);self.assertEqual(core.call_count,1);self.assertEqual(cloud.call_count,1)
    with store.connect(self.db) as c:
      self.assertEqual(len(store.list_reports(c,'alice')),1);self.assertEqual(store.get_user(c,'alice')['quota_used'],1);self.assertEqual(c.execute('SELECT SUM(ds_calls) FROM usage_log').fetchone()[0],1)
 def test_inflight_same_scope_cannot_start_second_orchestration(self):
    u=ict.USER.set('alice')
    try:
      token=self.prepared();ids=[REPORT.claims[0].claim.id];t=ict.register(token,ids,{},queue.Queue(),{});t['state']='running'
      self.assertRaises(ValueError,ict.register,token,ids,{},queue.Queue(),{})
      self.assertEqual(len(ict.TICKETS),1)
      t['state']='failed';t['flow_lock'].release()
    finally:ict.USER.reset(u)
 def test_changed_materials_cannot_recover_a_different_scope_after_restart(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core),patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok):self.verify_request(token,cookie)
    ict.TICKETS.clear();serve.PREPARED.clear()
    with patch.object(ict,'ORIGINAL_VERIFY') as core,patch.object(ict.runtime_client,'invoke') as cloud:
      code,_,_=self.request('/api/verify/stream',{'token':token,'claimIds':[REPORT.claims[0].claim.id],'githubMaterials':{'changed':True}},cookie,{'X-Underbox-Consent':'1'})
    self.assertEqual(code,404);core.assert_not_called();cloud.assert_not_called()
 def test_expired_completed_journal_is_not_reused(self):
    token=self.prepared();cookie=self.login()
    with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core),patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok):self.verify_request(token,cookie)
    path=next((self.db.parent/'ict-tasks').glob('*.json'));data=json.loads(path.read_text());data['created_at']=time.time()-ict.TASK_TTL-1;path.write_text(json.dumps(data));ict.TICKETS.clear();serve.PREPARED.clear()
    code,_,_=self.request('/api/verify/stream',{'token':token,'claimIds':[REPORT.claims[0].claim.id]},cookie,{'X-Underbox-Consent':'1'})
    self.assertEqual(code,404);self.assertFalse(path.exists())
 def test_no_tool_execution_cannot_become_complete(self):
    u=ict.USER.set('alice');q=queue.Queue()
    try:
      with patch.object(ict.runtime_client,'invoke',return_value=Stream(b'data: {"event":"summary_response","content":"SYNTHETIC_PROVIDER_INTERNAL_NARRATIVE"}\n')):
        ict.verify_selected(self.prepared(),[REPORT.claims[0].claim.id],q,{})
      rows=[]
      while not q.empty():rows.append(q.get())
      self.assertTrue(any(e and e.get('failed') for e in rows));self.assertFalse(any(e and e.get('report') for e in rows));self.assertNotIn('SYNTHETIC_PROVIDER_INTERNAL_NARRATIVE',json.dumps(rows))
    finally:ict.USER.reset(u)
 def test_history_activation_restores_matching_qa_context(self):
    with store.connect(self.db) as c:
      rid=store.save_report(c,'alice',serve.to_presentation(REPORT));c.commit()
    ict.save_context(rid,{'oby':REPORT,'profile':PROFILE});cookie=self.login()
    self.assertEqual(self.request('/api/history/'+str(rid)+'/activate',{},cookie)[0],200);self.assertEqual(serve.user_state('alice')['oby'].candidate_name,'合成测试甲')
    self.assertEqual(self.request('/api/history/'+str(rid)+'/activate',{},self.login('bob'))[0],404)
 def test_report_qa_stream_and_no_report_mutation(self):
    state=serve.user_state('alice');state.update(oby=REPORT,presentation=serve.to_presentation(REPORT));before=json.dumps(state['presentation'],sort_keys=True)
    with patch.object(serve,'stream_answer',return_value=iter([{'delta':'仅供人工复核'}])):
      code,h,raw=self.request('/api/ask/stream',{'question':'还缺什么材料','webSearch':False},self.login(),{'X-Underbox-Consent':'1'})
    self.assertEqual(code,200);self.assertIn('仅供人工复核',raw.decode());self.assertEqual(before,json.dumps(state['presentation'],sort_keys=True))
 def test_scope_survives_http_owner_release(self):
    meter=type(METER)();s=meter.open_scope();self.assertTrue(meter.retain_scope(s));meter.release_scope(s)
    with meter.bind(s):meter.record_llm('deepseek',{'prompt_tokens':2,'completion_tokens':3})
    self.assertEqual(meter.snapshot(s)['deepseek']['calls'],1);meter.release_scope(s);self.assertNotIn(s,meter._buckets)
 def test_voice_format_rejected_without_provider_call(self):
    serve.user_state('alice')['oby']=REPORT
    self.assertEqual(self.request('/api/voice/asr',b'invalid',self.login(),{'X-Underbox-Consent':'1','Content-Type':'audio/wav'})[0],413)
 def test_voice_asr_and_tts_success_with_synthetic_audio(self):
    import wave
    audio=io.BytesIO()
    with wave.open(audio,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*1600)
    payload=audio.getvalue();serve.user_state('alice')['oby']=REPORT;c=self.login()
    with patch('app.deepseek_client.DeepSeekClient') as client:
      asr=client.return_value.asr;asr.return_value={'text':'合成离线问题','duration_ms':100}
      code,h,body=self.request('/api/voice/asr',payload,c,{'X-Underbox-Consent':'1','Content-Type':'audio/wav'})
    self.assertEqual(code,200);self.assertEqual(json.loads(body)['text'],'合成离线问题');self.assertEqual(asr.call_count,1)
    with patch('app.deepseek_client.DeepSeekClient') as client:
      tts=client.return_value.tts;tts.return_value=payload
      code,h,body=self.request('/api/voice/tts',{'text':'合成离线回答'},c,{'X-Underbox-Consent':'1'})
    self.assertEqual(code,200);self.assertEqual(h['Content-Type'],'audio/wav');self.assertEqual(body,payload);self.assertEqual(tts.call_count,1)
 def test_online_qa_passes_actual_source_context_without_rewriting_report(self):
    state=serve.user_state('alice');state.update(oby=REPORT,presentation=serve.to_presentation(REPORT))
    source={'sources':[{'id':'W1','url':'https://example.invalid/official','title':'合成来源'}],'limitations':[]}
    with patch.dict(os.environ,{'DEEPSEEK_QA_NATIVE':'0'}),patch('app.qa_search.search_question',return_value=source) as search,patch.object(serve,'stream_answer',return_value=iter([{'delta':'参见 W1'}])) as model:
      code,h,data=self.request('/api/ask/stream',{'question':'补充公开资料','webSearch':True},self.login(),{'X-Underbox-Consent':'1'})
    self.assertEqual(code,200);self.assertEqual(search.call_count,1);self.assertEqual(model.call_args.args[2],source);self.assertIn('合成来源',data.decode());self.assertEqual(state['oby'],REPORT)
 def test_git_supplement_only_explicit_project(self):
    from app.github_audit import supplement_claims
    c=Claim(id='project',raw_text='课程项目开发',raw_locator='合成摘录',category='项目',date_label='2025',elements=[])
    self.assertRaises(ValueError,supplement_claims,[c],{'project':{'urls':['https://github.com/example/repo']}})
 def debug_bootstrap(self):
    code,headers,raw=self.request('/api/session');self.assertEqual(code,200)
    return json.loads(raw),headers.get('Set-Cookie','').split(';')[0]
 def test_debug_bootstrap_reuses_isolated_secure_cookie(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1','UNDERBOX_ICT_DEBUG_QUOTA':'5'}):
      d,c=self.debug_bootstrap();self.assertTrue(d['authed']);self.assertTrue(d['user']['debug']);self.assertEqual(d['user']['role'],'user');self.assertEqual(d['user']['quota']['total'],5)
      again=json.loads(self.request('/api/session',cookie=c)[2]);self.assertEqual(d['user']['id'],again['user']['id'])
      self.assertEqual(self.request('/api/history',cookie=c)[0],200)
 def test_debug_accounts_cannot_read_other_history_or_admin(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      a,ca=self.debug_bootstrap();b,cb=self.debug_bootstrap();self.assertNotEqual(a['user']['id'],b['user']['id'])
      with store.connect(self.db) as conn:
        rid=store.save_report(conn,a['user']['id'],serve.to_presentation(REPORT));conn.commit()
      self.assertEqual(len(json.loads(self.request('/api/history',cookie=cb)[2])['items']),0)
      self.assertEqual(self.request('/api/history/'+str(rid)+'/export',cookie=cb)[0],404)
      self.assertEqual(self.request('/api/admin/users',cookie=ca)[0],403)
      self.assertEqual(self.request('/api/admin/settings',{},ca)[0],403)
 def test_debug_sample_is_synthetic_and_not_team_material(self):
    import ict_debug
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      d,c=self.debug_bootstrap();code,h,b=self.request('/api/ict/sample',cookie=c)
    self.assertEqual(code,200);self.assertEqual(json.loads(b),{'text':ict_debug.SAMPLE,'mode':'synthetic'})
 def test_debug_explicit_admin_entry_does_not_auto_login(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      self.assertFalse(json.loads(self.request('/api/session?login=1')[2])['authed'])
      d,c=self.debug_bootstrap();self.assertFalse(json.loads(self.request('/api/session?login=1',cookie=c)[2])['authed'])
      admin=self.login('admin');self.assertEqual(json.loads(self.request('/api/session?login=1',cookie=admin)[2])['user']['role'],'admin')
 def test_debug_disable_revokes_guest_access_but_preserves_member(self):
    member=self.login()
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):d,c=self.debug_bootstrap()
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'0'}):
      self.assertFalse(json.loads(self.request('/api/session',cookie=c)[2])['authed']);self.assertEqual(self.request('/api/history',cookie=c)[0],401)
      self.assertTrue(json.loads(self.request('/api/session',cookie=member)[2])['authed'])
 def test_debug_expired_session_cannot_read_reports(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      d,c=self.debug_bootstrap()
      with store.connect(self.db) as conn:conn.execute('UPDATE ict_debug_users SET expires_at=0 WHERE user_id=?',(d['user']['id'],));conn.commit()
      self.assertEqual(self.request('/api/history',cookie=c)[0],401)
      fresh,newcookie=self.debug_bootstrap();self.assertNotEqual(fresh['user']['id'],d['user']['id'])
 def test_debug_keeps_consent_and_quota_guards(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      d,c=self.debug_bootstrap();self.assertEqual(self.request('/api/split/stream',{},c)[0],403)
      with store.connect(self.db) as conn:store.set_quota(conn,d['user']['id'],0);conn.commit()
      self.assertEqual(self.request('/api/verify/stream',{'token':'x','claimIds':['c1']},c,{'X-Underbox-Consent':'1'})[0],402)
 def test_debug_bootstrap_rejects_cross_origin_and_rate_limit(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      self.assertEqual(self.request('/api/session',extra={'Origin':'https://evil.invalid'})[0],403)
      with patch.object(self.acc,'allow_attempt',return_value=False):
        code,h,b=self.request('/api/session');self.assertEqual(code,429);self.assertNotIn('Set-Cookie',h)
 def test_debug_guest_can_complete_cloud_flow_without_admin_context(self):
    with patch.dict(os.environ,{'UNDERBOX_ICT_DEBUG_GUEST':'1'}):
      d,c=self.debug_bootstrap();uid=d['user']['id'];token=self.prepared(uid)
      with patch.object(ict,'ORIGINAL_VERIFY',side_effect=fake_core),patch.object(ict.runtime_client,'invoke',side_effect=cloud_ok):
        code,h,raw=self.request('/api/verify/stream',{'token':token,'claimIds':[REPORT.claims[0].claim.id]},c,{'X-Underbox-Consent':'1'})
      events=[json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ')];done=next(e for e in events if e.get('done'))
      self.assertEqual(code,200);self.assertIn('agentarts',done['report']);self.assertFalse(done.get('failed',False))
      with store.connect(self.db) as conn:
        self.assertEqual(len(store.list_reports(conn,uid)),1);self.assertEqual(len(store.list_reports(conn,'admin')),0);self.assertEqual(store.get_user(conn,uid)['quota_used'],1)
if __name__=='__main__':unittest.main(verbosity=2)

"""Behavioral migration checks. Temporary accounts, fake providers, loopback only."""
import contextlib,http.client,io,json,os,queue,socket,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'underbox'),str(ROOT/'open_box')]
os.environ.update(UNDERBOX_ICT_ENABLE='1',HUAWEICLOUD_SDK_AK='synthetic-ak',HUAWEICLOUD_SDK_SK='synthetic-sk')
import serve,accounts,store,ict_extension as ict
from app.schema import Report,Claim
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

def cloud_ok(query,session_id):
    data=json.loads(query.split('\n',1)[1]);assert session_id
    assert set(data)=={'action','resume_text','consent'}
    request_id=data['resume_text'].splitlines()[0].split(' ',1)[1]
    ict.tool_action(data);result=ict.tool_action({'action':'status','job_id':request_id})
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

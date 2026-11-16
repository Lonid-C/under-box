/* Preserves original report rendering; no synthetic verification or progress. */
(()=>{
const byId=id=>document.getElementById(id);
const api='__ICT_BASE__api/';
window.ictCostBudget=function(info){
 const box=byId('ictBudgetStatus');box.hidden=!info;if(!info)return;
 if(info.enforced===false){
  box.textContent='金额预算限制已关闭；'+(info.estimatedCny==null?'处理材料后显示费用估算。':
   '当前已计价部分估算 ¥'+Number(info.estimatedCny).toFixed(2)
   +'（含华为云预留 ¥'+Number(info.cloudAllowanceCny).toFixed(2)+'）。')
   +(info.estimateIncomplete?'部分调用未计入估算。':'')
   +(info.stopped?'已达到搜索安全上限，停止新增请求。':'不再因整份或逐条金额额度停止调用。')
   +'实际费用以各平台账单为准；主动问答和语音另计。';
  return;
 }
 box.textContent=`本份预算估算 ¥${Number(info.estimatedCny).toFixed(2)} / ¥${Number(info.limitCny).toFixed(2)}（含华为云预留 ¥${Number(info.cloudAllowanceCny).toFixed(2)}）。`
  +(info.stopped?'已停止新增付费请求，保留已有结果。':'约 ¥3 后减少备用补查。')
  +'实际费用以各平台账单为准；主动问答和语音另计。';
};
window.ictBudgetConfig=function(){
 if(typeof fetch!=='function')return;
 return fetch(api+'ict/status').then(r=>r.json()).then(d=>{
  if(d.retrieval?.expanded && document.body.dataset.stage!=='app')window.ictCostBudget({
   enforced:d.retrieval.cost_budget_enforced!==false,estimatedCny:null,
   limitCny:d.retrieval.estimated_budget_cny,cloudAllowanceCny:d.retrieval.cloud_allowance_cny});
 }).catch(()=>{});
};
window.ictBudgetConfig();
if(typeof applyReport==='function'){
 const previousApply=applyReport;
 applyReport=function(report,...args){const result=previousApply(report,...args);window.ictCostBudget(report?.costBudget || null);return result;};
}
byId('ictSampleButton').addEventListener('click',async()=>{
 byId('ictSampleButton').disabled=true;byId('ictTextButton').disabled=true;
 try{const r=await fetch(api+'ict/sample');const d=await r.json();if(!r.ok)throw new Error(d.error || '请先登录');byId('ictResumeText').value=d.text;byId('ictTextStatus').textContent=d.mode==='synthetic'?'已载入虚构调试示例，尚未发送给模型。':'已载入授权范围的两条赛事自述，尚未发送给模型。';}
 catch(e){byId('ictTextStatus').textContent=e.message;}
 finally{byId('ictSampleButton').disabled=false;byId('ictTextButton').disabled=false;}
});
byId('ictTextButton').addEventListener('click',()=>{
 if(!byId('ictConsent').checked){byId('ictTextStatus').textContent='请先勾选材料处理授权。';return;}
 const text=byId('ictResumeText').value.trim();
 if(!text){byId('ictTextStatus').textContent='请先粘贴脱敏文字。';return;}
 byId('ictTextStatus').textContent='正在将已确认摘录交给服务器整理；核验范围在下一步勾选。';
 accept('脱敏简历摘录.txt',new File([text],'脱敏简历摘录.txt',{type:'text/plain'}));
});
const dialog=byId('ictHistoryDialog');
byId('ictHistoryClose').addEventListener('click',()=>dialog.close());
async function openHistory(){
 dialog.showModal();if(QUERY_STREAMS.size){byId('ictHistoryStatus').textContent='请等待当前任务结束后回看历史。';return;}const list=byId('ictHistoryItems');list.replaceChildren();byId('ictHistoryStatus').textContent='读取当前账户的核验记录…';
 try{
  const r=await fetch(api+'history');const d=await r.json();if(!r.ok)throw new Error(d.error || '读取失败');
  byId('ictHistoryStatus').textContent=d.items.length?'仅显示本账户最近十次成功核验。':'暂无已完成的核验记录。';
  for(const item of d.items){
   const row=document.createElement('div');row.className='ict-history-row';const info=document.createElement('div');
   const title=document.createElement('b');title.textContent=item.candidateName+' · '+item.claims+' 项';const detail=document.createElement('small');detail.textContent=item.reportNo+' / '+item.createdAt;info.append(title,detail);
   const actions=document.createElement('div');const open=document.createElement('button');open.className='ghost';open.textContent='查看报告';
   open.addEventListener('click',async()=>{try{const r=await fetch(api+'history/'+item.id+'/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const d=await r.json();if(!r.ok)throw new Error(d.error);applyReport(d.report,{restored:true});document.body.dataset.stage='app';runEnter();clearInterval(StatusDock.timer);StatusDock.timer=null;byId('spLog').replaceChildren();byId('spSteps').replaceChildren();byId('spTitle').textContent='历史核验记录';byId('spTime').textContent='';byId('spFoot').textContent='本次只回看已保存结果，没有重新调用模型或搜索。';try{sessionStorage.removeItem('ub-query-ended:'+(ACCOUNT?.id || 'local'));}catch{}dialog.close();}catch(e){byId('ictHistoryStatus').textContent=e.message;}});
   const json=document.createElement('a');json.href=api+'history/'+item.id+'/export?format=json';json.textContent=' JSON ↓';json.className='navlink';
   const md=document.createElement('a');md.href=api+'history/'+item.id+'/export?format=md';md.textContent=' Markdown ↓';md.className='navlink';actions.append(open,json,md);row.append(info,actions);list.append(row);
  }
 }catch(e){byId('ictHistoryStatus').textContent=e.message;}
}
byId('ictHistoryButton').addEventListener('click',openHistory);
byId('ictHeroHistoryButton').addEventListener('click',openHistory);
const oldRenderPick=renderPick;
const materials=new Map();let materialScope=null;
renderPick=function(){if(materialScope!==PICK.token){materials.clear();materialScope=PICK.token;}oldRenderPick();for(const c of PICK.claims){
 if(!c.githubMaterialEligible)continue;
 const box=document.createElement('details');box.className='ict-github-material';const summary=document.createElement('summary');summary.textContent=c.id+' · 补充本条 GitHub 公开材料';
 const account=document.createElement('textarea');account.placeholder='本人 GitHub 账号主页 URL，每行一个，最多2个';account.setAttribute('aria-label',c.id+' 本人 GitHub 账号');
 const urls=document.createElement('textarea');urls.placeholder='本项目仓库 / PR / commit URL，每行一个，最多6个';urls.setAttribute('aria-label',c.id+' GitHub 项目链接');
 const saved=materials.get(c.id) || {};account.value=saved.accounts || '';urls.value=saved.urls || '';
 for(const input of [account,urls])input.addEventListener('input',()=>materials.set(c.id,{accounts:account.value,urls:urls.value}));
 const note=document.createElement('p');note.textContent='只核对原始简历已明确提及 GitHub 的这一项目，不扩展调查其他仓库。';box.append(summary,note,account,urls);byId('pickBlocks').append(box);
}};
window.ictGithubMaterials=()=>{const result={};if(materialScope!==PICK.token)return result;for(const c of PICK.claims){const m=materials.get(c.id);if(m && PICK.selected.has(c.id) && c.githubMaterialEligible){const accounts=m.accounts.split(/\s+/).filter(Boolean),urls=m.urls.split(/\s+/).filter(Boolean);if(accounts.length || urls.length)result[c.id]={accounts,urls};}}return result;};
})();

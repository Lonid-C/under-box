/* UnderBOX interface primitives. Text from reports/models always uses textContent. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const create = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'text') node.textContent = value;
      else if (key === 'class') node.className = value;
      else node.setAttribute(key, value);
    }
    children.flat().filter(x => x != null).forEach(x => node.append(x));
    return node;
  };
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const icons = {
    check:'<path d="m5 12 4 4L19 6"/>', info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
    close:'<path d="m6 6 12 12M6 18 18 6"/>', arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
    copy:'<rect x="8" y="8" width="12" height="12" rx="3"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
    spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    shield:'<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Z"/><path d="m8 12 3 3 5-6"/>',
    help:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3v.1"/>'
  };
  const icon = name => {
    const span = create('span', {class:'ub-icon', 'aria-hidden':'true'});
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + (icons[name] || icons.info) + '</svg>';
    return span;
  };
  let toastRoot, tooltip, initialized = false, activeDialog = null, tour = null, tooltipTimer, lastReport = '', boxyDrag = null;
  const timers = new WeakMap();

  function toast(message, type = 'info') {
    if (!toastRoot) return;
    while (toastRoot.children.length >= 3) toastRoot.firstChild.remove();
    const item = create('div', {class:'ub-toast ' + type, role:type === 'error' ? 'alert' : 'status'},
      icon(type === 'success' ? 'check' : 'info'), create('span', {text:message}));
    const close = create('button', {class:'ub-icon-button',type:'button','aria-label':'关闭提示'}, icon('close'));
    close.onclick = () => { clearTimeout(timers.get(item)); item.remove(); };
    item.append(close); toastRoot.append(item);
    const schedule = () => timers.set(item, setTimeout(() => { item.classList.add('leaving'); setTimeout(() => item.remove(), 220); }, type === 'error' ? 6500 : 3800));
    item.addEventListener('mouseenter', () => clearTimeout(timers.get(item)));
    item.addEventListener('mouseleave', schedule);
    schedule();
  }

  function place(card, anchor, gap = 12) {
    const rect = anchor?.getBoundingClientRect();
    const w = Math.min(card.offsetWidth || 340, innerWidth - 32);
    let left = rect ? Math.max(16, Math.min(rect.left, innerWidth - w - 16)) : (innerWidth - w) / 2;
    let top = rect ? rect.bottom + gap : (innerHeight - card.offsetHeight) / 2;
    if (top + card.offsetHeight > innerHeight - 16) top = rect ? rect.top - card.offsetHeight - gap : 16;
    card.style.left = left + 'px';
    card.style.top = Math.max(16, top) + 'px';
  }
  function trap(event, node) {
    if (event.key !== 'Tab') return;
    const items = [...node.querySelectorAll('button,a[href],input,select,textarea,[tabindex="0"]')].filter(n => !n.disabled && n.getClientRects().length);
    if (!items.length) return;
    const first = items[0], last = items.at(-1);
    if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }
  function confirmAction({anchor, title, message, confirmText = '确认', input = null}) {
    activeDialog?.close(null);
    return new Promise(resolve => {
      const previous = document.activeElement;
      const backdrop = create('div', {class:'ub-dialog-backdrop'});
      const card = create('section', {class:'ub-confirm' + (input ? ' with-input' : ''),role:'alertdialog','aria-modal':'true','aria-labelledby':'ubConfirmTitle','aria-describedby':'ubConfirmMessage'});
      const header = create('div', {class:'ub-confirm-head'}, icon('info'), create('h3',{id:'ubConfirmTitle',text:title}));
      card.append(header,create('p',{id:'ubConfirmMessage',text:message}));
      let field, validation;
      if (input) {
        field = create('input', {class:'ub-dialog-input',type:'number',min:'-1',step:'1','aria-label':'配额次数'});
        field.value = input.value;
        validation = create('p', {class:'ub-field-error',role:'alert'});
        card.append(field, validation);
      }
      const actions = create('div',{class:'ub-dialog-actions'});
      const cancel = create('button',{type:'button',class:'ub-button secondary',text:'取消'});
      const okay = create('button',{type:'button',class:'ub-button',text:confirmText});
      actions.append(cancel,okay);card.append(actions);
      backdrop.append(card);document.body.append(backdrop);document.body.classList.add('ub-dialog-open');place(card,anchor);
      const reposition=()=>place(card,anchor);
      addEventListener('resize',reposition);
      const close = value => { backdrop.remove();document.body.classList.remove('ub-dialog-open');removeEventListener('resize',reposition);activeDialog = null;previous?.focus();resolve(value); };
      activeDialog = {close,node:card};
      cancel.onclick = () => close(null);
      backdrop.addEventListener('click',e => {if(e.target === backdrop) close(null);});
      const submit = () => {
        if (input && !Number.isInteger(Number(field.value))) {validation.textContent='请输入整数。';field.focus();return;}
        if (input && (field.value.trim() === '' || Number(field.value) < -1)) {validation.textContent='请输入 -1 或非负整数。';field.focus();return;}
        close(input ? Number(field.value) : true);
      };
      okay.onclick = submit;
      field?.addEventListener('keydown',e => {if(e.key==='Enter')submit();});
      (field || cancel).focus();
    });
  }

  class InkStream {
    static active = new Set();
    constructor(host, onPaint = () => {}) {
      this.host=host;this.onPaint=onPaint;this.target='';this.cursor=0;this.frame=0;this.last=0;this.started=false;this.waiters=[];this.closed=false;
      this.reduce=reduced();InkStream.active.add(this);
      host.setAttribute('aria-busy','true');
    }
    status(text) {
      if (!this.started) {this.host.textContent=text;this.host.classList.add('ink-waiting');}
    }
    update(text) {
      if(this.closed)return;
      if (!this.started) {this.host.replaceChildren();this.host.classList.remove('ink-waiting');this.started=true;}
      this.target=text;
      if (this.reduce) {this.host.textContent=text;this.cursor=text.length;this.onPaint();return;}
      if(!this.frame)this.frame=requestAnimationFrame(t=>this.tick(t));
    }
    tick(time) {
      this.frame=0;
      if(this.closed || !this.host.isConnected){this.cancel();return;}
      const pending=this.target.slice(this.cursor);
      if(!pending){this.complete();return;}
      const elapsed=this.last ? Math.min(65,time-this.last) : 32;
      this.last=time;
      const chars=Array.from(pending);
      const rate=Math.max(this.finishing ? 350 : 65,Math.min(1500,chars.length*(this.finishing ? 12 : 5)));
      const count=Math.min(chars.length,Math.max(1,Math.ceil(elapsed*rate/1000)));
      const text=chars.slice(0,count).join('');
      const part=create('span',{class:'ink-fragment',text});
      this.host.append(part);this.cursor+=text.length;
      part.addEventListener('animationend',()=>{if(part.isConnected){part.replaceWith(document.createTextNode(text));this.host.normalize();}},{once:true});
      this.onPaint();
      if(this.cursor<this.target.length)this.frame=requestAnimationFrame(t=>this.tick(t));
      else this.complete();
    }
    complete(){this.last=0;this.waiters.splice(0).forEach(resolve=>resolve());}
    finish() {
      this.finishing=true;
      if(this.closed || this.cursor>=this.target.length){this.end();return Promise.resolve();}
      return new Promise(resolve=>this.waiters.push(()=>{this.end();resolve();}));
    }
    end(){this.host.setAttribute('aria-busy','false');InkStream.active.delete(this);}
    cancel({flush=false}={}) {
      if(this.closed)return;
      this.closed=true;cancelAnimationFrame(this.frame);this.frame=0;
      if(flush){this.host.textContent=this.target;this.cursor=this.target.length;}
      this.complete();this.end();
    }
    static cancelAll(){for(const item of [...InkStream.active])item.cancel();}
  }

  function sync() {
    if (!initialized) return;
    const report = typeof REPORT !== 'undefined' ? REPORT : null;
    const stage = document.body.dataset.stage;
    const heading = byId('workspaceTitle'), sub = byId('workspaceSubtitle');
    const picking = document.body.hasAttribute('data-picking');
    const restored = !!report && typeof Viewer !== 'undefined' && !Viewer.url;
    byId('intake')?.classList.toggle('report-restored',restored);
    if(byId('restoredSource'))byId('restoredSource').hidden=!restored;
    const inputTitle=byId('intakeTitle');
    if(restored && inputTitle)inputTitle.textContent='输入材料';
    else if(inputTitle?.textContent==='输入材料')inputTitle.textContent=byId('intake').dataset.mode==='url'?'粘贴个人主页':'上传简历';

    if(heading) heading.textContent=report ? (report.candidate || '候选人') + '的核验报告' : picking ? '确认本次核验范围' : '上传简历，开始核验';
    if(sub) sub.textContent=report ? (report.position && !report.position.includes('未标注') ? report.position + ' · ' : '') + report.claims.length + ' 条陈述'
      : picking ? '选择需要核验的条目。' : '选择需要核验的条目，查看检索结果。';
    byId('navExport')?.setAttribute('aria-disabled',String(!report));
    const steps=[...document.querySelectorAll('.ub-flow-step')];
    const current=report ? 3 : picking ? 1 : !byId('pending')?.hidden ? 2 : 0;
    steps.forEach((n,i)=>{n.classList.toggle('current',i===current);n.classList.toggle('complete',i<current);n.setAttribute('aria-current',i===current?'step':'false');});
    const riskCount=report?.risks?.length || 0;
    document.body.classList.toggle('has-input-risk',riskCount>0);
    const sources=byId('tierPanel'),risk=byId('risk-panel'),account=byId('adSheet');
    const open=sources&&!sources.hidden?sources:risk?.classList.contains('open')?risk:account&&!account.hidden?account:null;
    const backdrop=byId('ubDrawerBackdrop');
    if(backdrop){backdrop.hidden=!open;backdrop._panel=open;}
    document.body.classList.toggle('ub-drawer-open',!!open);
    if(open){open.setAttribute('role','dialog');open.setAttribute('aria-modal','true');}
    if(stage !== 'gate') requestAnimationFrame(() => boxyDrag?.refresh());
    if(stage !== 'gate' && !tour) maybeTour();
    if(report && report.reportNo !== lastReport){
      lastReport=report.reportNo;
      byId('bocyLauncherNote').textContent='报告已生成';
    }
    if(!report && byId('bocyLauncherNote'))byId('bocyLauncherNote').textContent='你的核验小助手';
  }

  function closeDrawers() {
    byId('tierPanel').hidden=true;
    byId('risk-panel').classList.remove('open');
    byId('risk-btn').setAttribute('aria-expanded','false');byId('risk-btn').textContent='查看详情';
    byId('adSheet').hidden=true;
    sync();
  }
  function tourKey(){return 'underbox.tour.v2:' + (typeof ACCOUNT !== 'undefined' && ACCOUNT?.id || 'local');}
  let tourScheduled=false;
  function maybeTour(){
    if(tourScheduled)return;
    try{if(localStorage.getItem(tourKey()))return;}catch{}
    tourScheduled=true;
    setTimeout(()=>{if(document.body.dataset.stage!=='gate' && !tour && !activeDialog)startTour();},650);
  }
  function startTour() {
    closeDrawers(); activeDialog?.close(null);tour?.close(false);
    const hasReport=typeof REPORT !== 'undefined' && !!REPORT;
    const steps=hasReport?[
      ['header.site','欢迎使用 UnderBOX 核验台','这里显示候选人的核验结果。'],
      ['#intake','查看原始材料','左侧显示输入材料，展开条目可查看原文和来源。'],
      ['#claims','查看条目详情','点击条目可查看证据、待补充信息和机构资料。'],
      ['#navTier','查看来源','查看来源分级，或从右上角导出报告。'],
      ['#chatToggle','有问题，问问boxy','可以就报告提问，也可以开启联网搜索。']
    ]:[
      ['#welcomeIntro','开始核验','上传简历后，选择需要核验的条目。'],
      ['#intake','上传简历或填写主页','选择简历文件，也可以切换到个人主页。'],
      ['#heroJourney','查看核验流程','上传材料、选择条目后，系统会检索并生成报告。'],
      ['#chatToggle','你好，我是 boxy','报告生成后，可以在这里提问。'],
      ['#helpHero','重新查看引导','点击“使用指南”可以再次查看引导。']
    ];
    const previous=document.activeElement;
    const blocker=create('div',{class:'ub-tour-blocker'});
    const spot=create('div',{class:'ub-tour-spot'});
    const card=create('section',{class:'ub-tour-card',role:'dialog','aria-modal':'true','aria-labelledby':'ubTourTitle','aria-describedby':'ubTourBody'});
    document.body.append(blocker,spot,card);document.body.classList.add('ub-tour-active');
    let index=0;
    const position=()=>{
      const target=document.querySelector(steps[index][0]);
      const r=target?.getBoundingClientRect();
      if(r && r.width && r.height){spot.hidden=false;Object.assign(spot.style,{left:Math.max(5,r.left-6)+'px',top:Math.max(5,r.top-6)+'px',width:Math.min(innerWidth-10,r.width+12)+'px',height:Math.min(innerHeight-10,r.height+12)+'px'});}
      else spot.hidden=true;
      place(card,target,18);
    };
    const close=done=>{
      blocker.remove();spot.remove();card.remove();document.body.classList.remove('ub-tour-active');tour=null;
      removeEventListener('resize',position);removeEventListener('scroll',position,true);
      if(done)try{localStorage.setItem(tourKey(),'done');}catch{}
      previous?.focus();
    };
    const render=()=>{
      card.replaceChildren();
      const top=create('div',{class:'ub-tour-top'},create('span',{text:'快速上手'}),create('span',{text:(index+1)+' / '+steps.length}));
      const title=create('h3',{id:'ubTourTitle',text:steps[index][1]});
      const body=create('p',{id:'ubTourBody',text:steps[index][2]});
      const dots=create('div',{class:'ub-tour-dots'});
      steps.forEach((_,i)=>dots.append(create('i',{class:i===index?'on':''})));
      const actions=create('div',{class:'ub-tour-actions'});
      const skip=create('button',{type:'button',class:'ub-tour-skip',text:'跳过引导'});
      skip.onclick=()=>close(true);actions.append(skip);
      if(index){const back=create('button',{type:'button',class:'ub-button secondary',text:'上一步'});back.onclick=()=>{index--;render();};actions.append(back);}
      const next=create('button',{type:'button',class:'ub-button',text:index===steps.length-1?'开始使用':'下一步'});
      next.onclick=()=>{if(index===steps.length-1)close(true);else{index++;render();}};
      actions.append(next);card.append(top,title,body,dots,actions);
      const target=document.querySelector(steps[index][0]);
      if(target && target.getClientRects().length)target.scrollIntoView({block:'nearest',behavior:'instant'});
      position();next.focus();
    };
    tour={close,node:card};addEventListener('resize',position);addEventListener('scroll',position,true);render();
  }

  function exportMenu(event) {
    event.preventDefault();
    if(typeof REPORT === 'undefined' || !REPORT){toast('完成一次核验后，就可以导出报告。');return;}
    document.querySelector('.ub-export-menu')?.remove();
    const anchor=event.currentTarget;
    const menu=create('div',{class:'ub-export-menu',role:'menu','aria-label':'导出报告'});
    for(const [format,label,desc] of [['md','Markdown 报告','适合阅读、分享和继续整理'],['json','JSON 原始数据','保留完整的核验字段与来源']]){
      const button=create('button',{type:'button',role:'menuitem'},icon('copy'),create('span',{},create('b',{text:label}),create('small',{text:desc})));
      button.onclick=async()=>{
        menu.remove();
        try{
          const response=await fetch('/api/export?format='+format);
          if(!response.ok)throw new Error('报告暂时无法导出，请稍后重试。');
          const blob=await response.blob(),url=URL.createObjectURL(blob);
          const a=create('a',{href:url,download:((typeof REPORT !== 'undefined' && REPORT?.reportNo)||'UnderBOX-report').replace(/[^a-zA-Z0-9_-]/g,'_')+'.'+format});
          a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
          toast('报告已准备好，浏览器将开始下载。','success');
        }catch(error){toast(error.message,'error');}
      };
      menu.append(button);
    }
    document.body.append(menu);place(menu,anchor,10);menu.querySelector('button').focus();
    menu.addEventListener('keydown',event=>{
      const items=[...menu.querySelectorAll('button')],current=items.indexOf(document.activeElement);
      if(event.key==='ArrowDown' || event.key==='ArrowUp'){
        event.preventDefault();items[(current+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();
      }
      if(event.key==='Escape'){event.preventDefault();menu.remove();anchor.focus();}
    });
    const dismiss=e=>{if(!menu.contains(e.target)&&e.target!==anchor){menu.remove();document.removeEventListener('pointerdown',dismiss);}};
    setTimeout(()=>document.addEventListener('pointerdown',dismiss),0);
  }

  function showTooltip(target) {
    clearTimeout(tooltipTimer);tooltip.hidden=true;
    const text=target?.getAttribute('data-tooltip') || target?.getAttribute('title');
    if(!text || target.disabled)return;
    if(target.hasAttribute('title')){target.dataset.tooltip=text;target.removeAttribute('title');}
    tooltipTimer=setTimeout(()=>{
      if(!target.isConnected)return;
      tooltip.textContent=text;tooltip.hidden=false;
      const r=target.getBoundingClientRect();
      tooltip.style.left=Math.max(8,Math.min(innerWidth-tooltip.offsetWidth-8,r.left+r.width/2-tooltip.offsetWidth/2))+'px';
      tooltip.style.top=Math.max(8,r.top-tooltip.offsetHeight-9)+'px';
      target.setAttribute('aria-describedby','ubTooltip');
    },400);
  }
  function init() {
    if(initialized)return;initialized=true;
    toastRoot=create('div',{class:'ub-toasts','aria-live':'polite'});
    tooltip=create('div',{id:'ubTooltip',class:'ub-tooltip',role:'tooltip',hidden:''});
    const backdrop=create('div',{id:'ubDrawerBackdrop',class:'ub-drawer-backdrop',hidden:''});
    backdrop.onclick=closeDrawers;
    // Keep the fixed drawer outside the sticky header's stacking context.
    const sourcePanel=byId('tierPanel');
    if(sourcePanel){sourcePanel.setAttribute('aria-label','来源与依据');document.body.append(sourcePanel);}
    document.body.append(backdrop,toastRoot,tooltip);
    byId('navExport')?.addEventListener('click',exportMenu);
    byId('newFromSource')?.addEventListener('click',()=>byId('reset').click());
    ['helpHero','helpGuide'].forEach(id=>byId(id)?.addEventListener('click',startTour));
    byId('riskClose')?.addEventListener('click',closeDrawers);
    byId('processButton')?.addEventListener('click',()=>{
      if(typeof StatusDock !== 'undefined' && !byId('statusDock').hidden)StatusDock.open(true,{user:true});
      else toast(typeof REPORT !== 'undefined' && REPORT ? '这份报告的过程日志未保留在当前窗口，可展开条目查看证据与出处。' : '开始核验后，这里会显示真实检索过程。');
    });
    document.addEventListener('pointerover',e=>showTooltip(e.target.closest('[title],[data-tooltip]')));
    document.addEventListener('focusin',e=>showTooltip(e.target.closest('[title],[data-tooltip]')));
    document.addEventListener('pointerout',e=>{if(e.target.closest('[title],[data-tooltip]')){clearTimeout(tooltipTimer);tooltip.hidden=true;}});
    document.addEventListener('focusout',()=>{clearTimeout(tooltipTimer);tooltip.hidden=true;});
    document.addEventListener('click',async e=>{
      const copy=e.target.closest('[data-copy-answer]');
      if(copy){try{await navigator.clipboard.writeText(copy.closest('.msg').querySelector('.bub').innerText);toast('已复制回答。','success');}catch{toast('浏览器未允许复制，请选中文字后复制。','error');}}
    });
    document.addEventListener('keydown',e=>{
      if(e.isComposing || e.keyCode===229)return;
      const dialog=tour?.node || activeDialog?.node;
      if(dialog){trap(e,dialog);if(e.key==='Escape'){e.preventDefault();tour ? tour.close(true) : activeDialog.close(null);}return;}
      if(e.key==='Escape'){
        closeDrawers();document.querySelector('.ub-export-menu')?.remove();
        if(byId('chat')?.classList.contains('open'))byId('chatClose').click();
      }
      if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='j'&&document.body.dataset.stage!=='gate'){e.preventDefault();(byId('chat').classList.contains('open')?byId('chatClose'):byId('chatToggle')).click();}
      if(document.body.classList.contains('ub-drawer-open')){
        const panel=byId('ubDrawerBackdrop')._panel;if(panel)trap(e,panel);
      }
    });
    const launcher=byId('chatToggle');
    if (launcher && window.BoxyDrag) boxyDrag=window.BoxyDrag.mount(launcher, launcher.closest('.chat-dock'), window);
    const smallScreen=matchMedia('(max-width:700px)');
    const foldSummaries=()=>document.querySelectorAll('details.side-extra').forEach(node=>node.open=!smallScreen.matches);
    foldSummaries();smallScreen.addEventListener('change',foldSummaries);
    const observer=new MutationObserver(sync);
    observer.observe(document.body,{attributes:true,attributeFilter:['data-stage','data-picking','data-noreport']});
    for(const id of ['tierPanel','risk-panel','adSheet','pending','pick','chat']){
      const node=byId(id);if(node)observer.observe(node,{attributes:true,attributeFilter:['hidden','class']});
    }
    byId('chat')?.addEventListener('transitionend',sync);
    sync();
  }
  window.InkStream=InkStream;
  window.UnderboxUI={
    toast,confirm:confirmAction,prompt:options=>confirmAction({...options,input:{value:String(options.value)}}),
    sync,tour:startTour,icon,
    reportReady(report,{quiet=false}={}){
      const changed = report?.reportNo !== lastReport;
      sync();
      if(initialized && changed && !quiet)toast('核验报告已就绪，可展开条目查看出处。','success');
    },
    answerReady(node){
      const button=create('button',{class:'ub-copy-answer',type:'button','data-copy-answer':'','data-tooltip':'复制这条回答','aria-label':'复制回答'},icon('copy'),create('span',{text:'复制'}));
      node.append(button);
      if(!byId('chat').classList.contains('open'))toast('boxy 已回答。','success');
    }
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

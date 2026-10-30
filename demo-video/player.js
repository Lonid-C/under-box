'use strict';
(() => {
  const $ = id => document.getElementById(id), clamp = (x,a,b) => Math.max(a,Math.min(b,x));
  let data, scenes, duration=0, position=0, playing=false, last=0, started=false, sceneIndex=-1, lastBeat=-1, audio, nodes=new Set();
  const stage=$('stage'), shotNodes=[...document.querySelectorAll('.scene')];
  function fit(){const r=$('viewport').getBoundingClientRect(),s=Math.min(r.width/1920,r.height/1080);stage.style.transform=`translate(${(r.width-1920*s)/2}px,${(r.height-1080*s)/2}px) scale(${s})`;}
  addEventListener('resize',fit);fit();
  const clock=t=>{let s=Math.floor(t);return `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`};
  const lerp=(a,b,t)=>a+(b-a)*t, ease=t=>1-Math.pow(1-clamp(t,0,1),3);
  function recalc(){duration=0;scenes.forEach(s=>{s.start=duration;duration+=s.duration});$('seek').max=duration; $('startCover').querySelector('p').textContent='产品与技术演示 · '+clock(duration); $('chapter').replaceChildren(...scenes.map((s,i)=>{let o=document.createElement('option');o.value=i;o.textContent=`${String(i+1).padStart(2,'0')} · ${s.title}`;return o;}));}
  function tone(f,when,length,volume,type='sine'){
    if(!audio||audio.state!=='running'||!volume)return;
    const osc=audio.createOscillator(),g=audio.createGain();osc.type=type;osc.frequency.value=f;g.gain.setValueAtTime(0.0001,when);g.gain.exponentialRampToValueAtTime(Math.max(.0002,volume),when+.025);g.gain.exponentialRampToValueAtTime(.0001,when+length);osc.connect(g);g.connect(audio.destination);nodes.add(osc);osc.onended=()=>nodes.delete(osc);osc.start(when);osc.stop(when+length+.02);
  }
  function stopAudio(){for(const n of nodes){try{n.stop()}catch{}}nodes.clear();lastBeat=-1;}
  async function enableAudio(){try{audio ||= new (window.AudioContext||window.webkitAudioContext)();if(audio.state==='suspended')await audio.resume();}catch{}}
  function sound(index,local){if(!audio||audio.state!=='running')return;const now=audio.currentTime;
    const beat=Math.floor(position/.5);if(beat!==lastBeat){lastBeat=beat;if($('music').checked){const scale=[220,261.63,329.63,392,329.63,261.63,246.94,293.66];let vol=+$('musicVolume').value*.13;const endFade=clamp((duration-position)/5,0,1);tone(scale[beat%scale.length]*(index>3&&index<7?2:1),now,.32,vol*endFade,'sine');if(beat%8===0){tone(scale[(beat/8|0)%scale.length]/2,now,1.7,vol*.65*endFade,'triangle');}}}}
  function clickSound(){if(audio&&$('sfx').checked)tone(740,audio.currentTime,.12,+$('sfxVolume').value*.08,'sine');}
  function render(){
    const index=Math.max(0,scenes.findIndex((s,i)=>position>=s.start&&(position<s.start+s.duration||i===scenes.length-1)));const s=scenes[index];const local=clamp(position-s.start,0,s.duration), q=local/s.duration;
    if(index!==sceneIndex){if(playing&&sceneIndex>=0)clickSound();sceneIndex=index;}
    for(const node of shotNodes){const active=node.dataset.scene===s.kind;node.classList.toggle('active',active);if(active){const fade=Math.min(ease(local/.8),index===scenes.length-1?1:ease((s.duration-local)/.65));node.style.opacity=String(fade);const content=node.querySelectorAll('h1,h2,.copy,.eyebrow,.intro-mark,.intro-line,.intro-sub,.outro-mark,.outro-rule,.outro-bottom,.problem-cards,.rule-line,.claim-layout,.orchestra,.rule-layout,.context-images');content.forEach((el,i)=>{const a=ease((local-.12-i*.055)/.8);el.style.opacity=String(a);el.style.transform=`translateY(${(1-a)*22}px)`});const elements=node.querySelectorAll('.p-card,.claim-stack>div,.or-node,.or-center,.evidence-tags span,.rule-card,.status-list span,.context-card');elements.forEach((el,i)=>{const reveal=ease((local-.35-i*.22)/.75);el.style.opacity=String(reveal);el.style.transform=`translateY(${(1-reveal)*28}px)`});node.querySelectorAll('.orchestra path').forEach((path,i)=>{path.style.opacity=String(ease((local-.5-i*.14)/.8));path.style.strokeDashoffset=String(-local*14)});const device=node.querySelector('.device,.portrait-frame,.upload-frame');if(device){const intro=ease(local/1.2);device.style.opacity=String(intro);device.style.transform=`translateY(${(1-intro)*45}px) scale(${1.005+q*.035})`;}}
    }
    const sourceTime=local/s.duration*s.originalDuration;const cue=s.subtitles.find(c=>sourceTime>=c.start&&sourceTime<c.end)||(local===s.duration?s.subtitles.at(-1):null);$('subtitle').textContent=$('subs').checked&&cue?cue.text:'';$('vocalCue').hidden=!$('voice').checked;$('vocalCue').querySelector('span').textContent=cue?.text||'';
    $('seek').value=position;$('time').textContent=`${clock(position)} / ${clock(duration)}`;$('chapter').value=String(index);$('play').textContent=playing?'❚❚':'▶';$('play').setAttribute('aria-label',playing?'暂停':'播放');
  }
  function frame(t){if(playing){if(last)position=Math.min(duration,position+(t-last)/1000);last=t;render();sound(sceneIndex,position-scenes[sceneIndex].start);if(position>=duration){playing=false;last=0;stopAudio();render();}}requestAnimationFrame(frame)}
  function play(){if(!data)return;if(position>=duration)position=0;started=true;$('startCover').hidden=true;playing=true;last=0;enableAudio();render()}
  function pause(){playing=false;last=0;stopAudio();render()}
  function seekTo(t){position=clamp(t,0,duration);last=0;sceneIndex=-1;stopAudio();render()}
  $('start').onclick=play;$('play').onclick=()=>playing?pause():play();$('restart').onclick=()=>{seekTo(0);play()};
  $('seek').oninput=e=>seekTo(+e.target.value);$('chapter').onchange=e=>{started=true;$('startCover').hidden=true;seekTo(scenes[+e.target.value].start+1)};
  for(const id of ['subs','voice','music','sfx'])$(id).onchange=()=>{if(id==='music'&&!$(id).checked)stopAudio();render()};
  $('record').onclick=()=>{document.body.classList.add('recording');$('record').blur();if(!started)play();fit()};
  addEventListener('keydown',e=>{if(e.key==='Escape'){document.body.classList.remove('recording');fit();return}if(e.target.matches('input,select,textarea,button'))return;if(e.code==='Space'){e.preventDefault();playing?pause():play()}else if(e.key==='ArrowRight')seekTo(position+5);else if(e.key==='ArrowLeft')seekTo(position-5)});
  $('speedButton').onclick=()=>{const editor=$('durationEditor');editor.hidden=!editor.hidden;if(!editor.hidden){$('durationValue').value=scenes[sceneIndex].duration;$('durationError').textContent='';$('durationValue').focus()}};
  $('durationCancel').onclick=()=>{$('durationEditor').hidden=true};
  $('durationApply').onclick=()=>{const s=scenes[sceneIndex],old=s.duration,n=Number($('durationValue').value);if(!Number.isFinite(n)||n<6||n>90){$('durationError').textContent='请输入 6–90 秒';return}const fraction=(position-s.start)/old;s.duration=n;recalc();position=s.start+fraction*n;try{localStorage.setItem('underbox.video.durations',JSON.stringify(scenes.map(x=>x.duration)))}catch{}$('durationEditor').hidden=true;render()};

  function stamp(n){let ms=Math.round(n*1000);return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')},${String(ms%1000).padStart(3,'0')}`}
  const exportButton=document.createElement('button');exportButton.textContent='导出当前字幕';exportButton.onclick=()=>{let i=0;const out=scenes.flatMap(s=>s.subtitles.map(c=>`${++i}\n${stamp(s.start+c.start/s.originalDuration*s.duration)} --> ${stamp(s.start+c.end/s.originalDuration*s.duration)}\n${c.text}\n`)).join('\n');const a=document.createElement('a'),url=URL.createObjectURL(new Blob([out],{type:'text/plain;charset=utf-8'}));a.href=url;a.download='underbox-subtitles.srt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};$('speedButton').after(exportButton);
  Promise.resolve(window.UNDERBOX_TIMELINE).then(json=>{if(!json||!Array.isArray(json.scenes))throw new Error('无法读取时间轴');data=json;let saved=[];try{saved=JSON.parse(localStorage.getItem('underbox.video.durations')||'[]')}catch{}scenes=json.scenes.map((s,i)=>({...s,originalDuration:s.duration,duration:Number.isFinite(saved[i])&&saved[i]>=6&&saved[i]<=90?saved[i]:s.duration}));recalc();render();requestAnimationFrame(frame)}).catch(e=>{$('fatal').hidden=false;$('fatal').textContent='演示资源加载失败：'+e.message+'。请检查 timeline.js 和使用说明。'});
})();

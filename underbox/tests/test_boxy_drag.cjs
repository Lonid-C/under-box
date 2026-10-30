const test = require('node:test');
const assert = require('node:assert/strict');
const {mount} = require('../boxy-drag.js');
function setup(initial) {
  const callbacks = {}, viewCallbacks = {}, values = new Map();
  if (initial) values.set('underbox.boxy.position.v1', JSON.stringify(initial));
  const cls = new Set();
  const dock = {style:{}, getBoundingClientRect() { return {
    left: parseFloat(this.style.left) || 300, top: parseFloat(this.style.top) || 200, width:100, height:80
  }; }};
  const handle = {
    classList:{add:k=>cls.add(k),remove:k=>cls.delete(k)},
    getClientRects:()=>handle.visible ? [{}] : [], visible:true,
    setPointerCapture:id=>handle.captured=id,
    addEventListener:(type,fn)=>{(callbacks[type] ||= []).push(fn);}
  };
  const view = {innerWidth:500,innerHeight:400,localStorage:{
    getItem:key=>values.get(key) || null, setItem:(key,val)=>values.set(key,val)
  },addEventListener:(type,fn)=>{viewCallbacks[type]=fn;},setTimeout:()=>1,clearTimeout:()=>{}};
  const component = mount(handle,dock,view);
  const fire = (type,opts={}) => {
    const e={type,isPrimary:true,button:0,pointerId:1,clientX:300,clientY:200,detail:1,
      preventDefault(){this.defaultPrevented=true;},stopImmediatePropagation(){this.stopped=true;},...opts};
    for (const fn of callbacks[type]||[]) fn(e);
    return e;
  };
  return {view,handle,dock,fire,component,values,viewCallbacks,cls};
}
test('drag moves, clamps and persists position without opening chat',()=>{
  const x=setup();
  x.fire('pointerdown');
  x.fire('pointermove',{clientX:900,clientY:900});
  x.fire('pointerup',{clientX:900,clientY:900});
  assert.equal(x.dock.style.left,'392px');
  assert.equal(x.dock.style.top,'312px');
  assert.deepEqual(JSON.parse(x.values.get('underbox.boxy.position.v1')),{x:392,y:312});
  assert.equal(x.fire('click').stopped,true);
  assert.equal(x.cls.has('is-dragging'),false);
});
test('tap and keyboard click still open the launcher',()=>{
  const x=setup();
  x.fire('pointerdown',{pointerType:'touch'});
  x.fire('pointermove',{clientX:303,clientY:202,pointerType:'touch'});
  x.fire('pointerup',{pointerType:'touch'});
  assert.equal(x.fire('click').stopped,undefined);
  assert.equal(x.fire('click',{detail:0}).stopped,undefined);
  assert.equal(x.values.size,0);
});
test('saved position restores and stays in view after resize or reopening',()=>{
  const x=setup({x:350,y:290});
  x.component.refresh();
  assert.equal(x.dock.style.left,'350px');
  x.handle.visible=false; x.view.innerWidth=250; x.view.innerHeight=240;
  x.viewCallbacks.resize();
  assert.equal(x.dock.style.left,'350px');
  x.handle.visible=true; x.component.refresh();
  assert.equal(x.dock.style.left,'142px');
  assert.equal(x.dock.style.top,'152px');
});
test('cancelled touch drag never suppresses a later tap',()=>{
  const x=setup();
  x.fire('pointerdown',{pointerType:'touch'});
  x.fire('pointermove',{clientX:340,clientY:230,pointerType:'touch'});
  x.fire('pointercancel',{pointerType:'touch'});
  assert.equal(x.fire('click').stopped,undefined);
  assert.equal(x.values.size,0);
});

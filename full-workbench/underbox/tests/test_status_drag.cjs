const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');

function node(width, height) {
  const events = new Map();
  const classes = new Set();
  return {
    offsetWidth: width, offsetHeight: height, hidden: false, style: {},
    classList: {add: x => classes.add(x), remove: x => classes.delete(x)},
    addEventListener: (type, fn) => events.set(type, fn),
    fire: (type, event) => events.get(type)?.(event)
  };
}
const dock = node(120, 40), panel = node(390, 260), pill = node(120, 40), header = node(390, 40);
panel.querySelector = () => header;
const globals = node(0, 0);
const storage = new Map();
let offset = 0;
const ctx = vm.createContext({
  $: id => ({statusDock:dock, statusPanel:panel, statusPill:pill})[id],
  getComputedStyle: () => ({left:'24px', bottom:'24px', getPropertyValue: () => String(offset)}),
  innerWidth: 1000, innerHeight: 750,
  addEventListener: globals.addEventListener,
  localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}
});
vm.runInContext(html.slice(html.indexOf('const DockDrag = {'), html.indexOf('/* 读后端的 SSE')) +
                '\nDockDrag.bind(); globalThis.dragLogic = DockDrag;', ctx);
const pointer = (x, y, id = 1) => ({clientX:x, clientY:y, pointerId:id, button:0,
                                  currentTarget:header, target:{closest:() => null}});
header.fire('pointerdown', pointer(45, 500));
globals.fire('pointermove', pointer(46, 501));
assert.equal(panel.style.left,'24px');
globals.fire('pointermove', pointer(95, 470, 7));
assert.equal(panel.style.bottom,'24px');
globals.fire('pointermove', pointer(95, 450));
assert.equal(panel.style.left, '74px');
assert.equal(panel.style.bottom, '74px');
globals.fire('pointermove', pointer(145, 525));
assert.equal(panel.style.left, '124px');
assert.equal(panel.style.bottom, '8px');
globals.fire('pointerup', pointer(145, 525));
assert.deepEqual(JSON.parse(storage.get('ub.dockpos.v1')), {l:124,b:8});
header.fire('click', {detail:1, stopImmediatePropagation(){assert.fail('header canceled collapse');},
                      preventDefault(){assert.fail('header canceled collapse');}});

panel.hidden = true;
ctx.dragLogic.fit();
const pressPill = {...pointer(145, 650), currentTarget:pill};
pill.fire('pointerdown', pressPill);
globals.fire('pointermove', pointer(990, 580));
assert.equal(dock.style.left, '872px');
assert.equal(dock.style.bottom, '78px');
globals.fire('pointerup', pointer(990, 580));
let suppressed = false;
pill.fire('click', {detail:1, stopImmediatePropagation(){}, preventDefault(){suppressed = true;}});
assert.ok(suppressed, 'a release click after dragging the pill must not open it');
suppressed = false;
pill.fire('pointerdown', pressPill);
globals.fire('pointerup', pointer(145, 650));
pill.fire('click', {detail:1, stopImmediatePropagation(){}, preventDefault(){suppressed = true;}});
assert.ok(!suppressed, 'an ordinary click on the pill can still open it');

panel.hidden = false;
ctx.dragLogic.fit();
assert.equal(panel.style.left,'602px');
ctx.innerWidth = 390;
panel.offsetWidth = 360;
offset = 80;
globals.fire('resize');
assert.equal(panel.style.left,'22px');
assert.equal(panel.style.bottom,'158px');
header.fire('pointerdown', pointer(38, 400));
globals.fire('pointermove', pointer(38, 450));
assert.equal(panel.style.bottom,'108px');
header.fire('lostpointercapture', pointer(38, 450));
globals.fire('pointermove', pointer(38, 470));
assert.equal(panel.style.bottom,'108px');
console.log('Status panel and collapsed pill follow the active pointer and stay in the viewport.');

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = { module: { exports: {} } };
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../chatui/chatbean-splitter.js'),'utf8'),context);
const { mount, limits, preference, STORAGE_KEY } = context.module.exports;

function fixture(saved = null, scale = 1) {
  const values = new Map([['real-ui-settings-1','{"theme":"dark","labelDetails":false}']]);
  if(saved!==null)values.set(STORAGE_KEY,saved);
  function node() {
    const properties=new Map(),classes=new Set();
    return { attrs:{},events:{},children:[],captured:null,
      style:{setProperty(k,v){properties.set(k,v)},removeProperty(k){properties.delete(k)},getPropertyValue(k){return properties.get(k)||''}},
      classList:{add(k){classes.add(k)},remove(k){classes.delete(k)},contains(k){return classes.has(k)}},
      setAttribute(k,v){this.attrs[k]=v},addEventListener(k,fn){this.events[k]=fn},append(n){this.children.push(n)},
      setPointerCapture(id){this.captured=id},hasPointerCapture(id){return this.captured===id},releasePointerCapture(){this.captured=null},
      focus(){this.focused=true},remove(){this.removed=true},
    };
  }
  const app=node(),rail=node(),pane=node(),body=node();
  app.offsetWidth=1280;rail.offsetWidth=56;
  app.getBoundingClientRect=()=>({width:app.offsetWidth*scale});
  pane.getBoundingClientRect=()=>({width:(parseFloat(pane.style.getPropertyValue('--cb-user-list-width'))||224)*scale});
  const hostEvents={};let observer;
  const host={localStorage:{getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)},
    addEventListener:(k,fn)=>{hostEvents[k]=fn},removeEventListener:k=>delete hostEvents[k],
    ResizeObserver:class{constructor(fn){this.callback=fn;observer=this}observe(){}disconnect(){this.disconnected=true}}};
  const doc={body,createElement:()=>node(),querySelector:s=>({'.session-pane':pane,'.nav-rail':rail,'.app-window':app})[s]};
  const mounted=mount(doc,host),handle=mounted.element;
  const event=(type,props={})=>{const e={pointerId:1,button:0,isPrimary:true,clientX:300,preventDefault(){this.prevented=true},...props};handle.events[type](e);return e};
  return {values,app,pane,body,host,hostEvents,handle,mounted,event,observer:()=>observer,width:()=>Number(handle.attrs['aria-valuenow'])};
}

test('pointer capture keeps a drag active and saves only the released CSS width at 150% zoom',()=>{
  const f=fixture(null,1.5);f.event('pointerdown');assert.equal(f.handle.captured,1);
  f.event('pointermove',{clientX:450});assert.equal(f.width(),324);assert.equal(f.values.has(STORAGE_KEY),false);
  f.event('pointerup',{clientX:450});assert.equal(f.values.get(STORAGE_KEY),'324');
  assert.equal(f.body.classList.contains('cb-resizing-sidebar'),false);assert.equal(f.handle.captured,null);
  assert.equal(fixture('324').width(),324);
});
test('a narrower window limits actual width but restores the saved preference on expansion',()=>{
  const f=fixture('480');assert.equal(f.width(),480);
  f.app.offsetWidth=720;f.observer().callback();assert.equal(f.width(),304);
  assert.equal(f.values.get(STORAGE_KEY),'480');assert.ok(720-56-f.width()>=360);
  f.app.offsetWidth=1280;f.observer().callback();assert.equal(f.width(),480);
});
test('double click restores default and preserves the original settings record',()=>{
  const f=fixture('370');f.event('dblclick');assert.equal(f.width(),224);
  assert.equal(f.values.has(STORAGE_KEY),false);
  assert.equal(f.values.get('real-ui-settings-1'),'{"theme":"dark","labelDetails":false}');
});
test('arrow keys, limits and keyboard reset work without affecting other keys',()=>{
  const f=fixture();assert.equal(f.handle.attrs.role,'separator');assert.equal(f.handle.attrs['aria-controls'],f.pane.id);
  f.event('keydown',{key:'ArrowRight'});assert.equal(f.width(),232);
  f.event('keydown',{key:'ArrowLeft',shiftKey:true});assert.equal(f.width(),200);
  f.event('keydown',{key:'Home'});assert.equal(f.width(),180);
  f.event('keydown',{key:'End'});assert.equal(f.width(),480);
  f.event('keydown',{key:'Delete'});assert.equal(f.width(),224);
  assert.equal(f.event('keydown',{key:'a'}).prevented,undefined);
});
test('Escape, pointer cancellation and blur restore the pre-drag width without writing',()=>{
  for(const cancel of ['escape','pointercancel','blur']){
    const f=fixture('280');f.event('pointerdown');f.event('pointermove',{clientX:420});assert.equal(f.width(),400);
    if(cancel==='escape')f.event('keydown',{key:'Escape'});else if(cancel==='blur')f.hostEvents.blur();else f.event(cancel);
    assert.equal(f.width(),280);assert.equal(f.values.get(STORAGE_KEY),'280');assert.equal(f.handle.captured,null);
  }
});
test('secondary pointers and unrelated pointer events do not resize the list',()=>{
  const f=fixture();f.event('pointerdown',{button:2});f.event('pointermove',{clientX:450});assert.equal(f.width(),224);
  f.event('pointerdown');f.event('pointermove',{pointerId:2,clientX:450});f.event('pointerup',{pointerId:2});assert.equal(f.width(),224);
  f.event('pointermove',{clientX:310});assert.equal(f.width(),234);
});
test('corrupt stored widths are ignored and unavailable persistence does not block interaction',()=>{
  for(const raw of ['', 'NaN', 'Infinity', '-100', '99999','{}'])assert.equal(preference(raw),null);
  const f=fixture('garbage');assert.equal(f.width(),224);
  f.host.localStorage.setItem=()=>{throw new Error('disabled')};
  assert.doesNotThrow(()=>f.event('keydown',{key:'ArrowRight'}));assert.equal(f.width(),232);
});
test('tiny windows remain bounded and cleanup releases listeners and observer',()=>{
  for(const available of [0,100,400,700,2000]){const b=limits(available);assert.ok(b.min>=0&&b.max>=b.min&&b.max<=available)}
  const f=fixture();f.mounted.destroy();assert.equal(f.handle.removed,true);assert.equal(f.observer().disconnected,true);
  assert.deepEqual(f.hostEvents,{});
});

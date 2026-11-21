// Run from project root: node tests/test_alarm_clear.cjs
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
function element() {
  return {value:'', textContent:'', disabled:false, open:false, listeners:{},
    addEventListener(name, fn) {this.listeners[name]=fn;}, focus(){},
    showModal(){this.open=true;}, close(){this.open=false;this.listeners.close?.();}};
}
const ids = new Map(), listeners = {}, dispatched=[];
const get = id => {if(!ids.has(id))ids.set(id,element());return ids.get(id);};
let fetchImpl, request;
const context = {console, AbortSignal, Event, document:{getElementById:get,
  addEventListener(name,fn){listeners[name]=fn;}}, window:{dispatchEvent(e){dispatched.push(e.type);}},
  esc:s=>String(s).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;'),
  fetch:(url,options)=>{request={url,options};return fetchImpl();}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('gateway/web/static/alarm-clear.js','utf8'),context);
const button={dataset:{node:'0000000000000001',alarm:'42',label:'F-07-01'}};
function open(){listeners.click({target:{closest:()=>button},preventDefault(){}});get('clear-token').value='test-secret';}
async function run(){
  assert.equal(vm.runInContext('clearAlarmButton({state:"NORMAL",alarm_event_id:42},true)',context),'');
  assert.equal(vm.runInContext('clearAlarmButton({state:"ALARM",alarm_event_id:42},false)',context),'');
  assert(vm.runInContext('clearAlarmButton({state:"ALARM",alarm_event_id:42,dev_eui:"abc",label:"<bad>"},true)',context).includes('&lt;bad>'));
  open();get('clear-cancel').listeners.click();assert(!get('clear-dialog').open);assert.equal(get('clear-token').value,'');
  open();let resolve;fetchImpl=()=>new Promise(r=>{resolve=r;});
  const pending=get('clear-form').listeners.submit({preventDefault(){}});
  assert(get('clear-confirm').disabled);assert.equal(dispatched.length,0);assert(get('clear-dialog').open);
  assert.equal(request.options.headers.Authorization,'Bearer test-secret');
  assert.deepEqual(JSON.parse(request.options.body),{alarm_event_id:42});
  resolve({ok:true,json:async()=>({ok:true,cleared:true})});await pending;
  assert(!get('clear-dialog').open);assert.deepEqual(dispatched,['alarm-cleared']);assert.equal(get('clear-token').value,'');
  open();fetchImpl=async()=>({ok:false,json:async()=>({detail:'Unauthorized'})});
  await get('clear-form').listeners.submit({preventDefault(){}});
  assert(get('clear-dialog').open);assert.equal(get('clear-error').textContent,'Unauthorized');
  assert.equal(dispatched.length,1);assert(!get('clear-confirm').disabled);assert.equal(get('clear-token').value,'');
  open();fetchImpl=async()=>{throw Object.assign(new Error('Timeout'),{name:'TimeoutError'});};
  await get('clear-form').listeners.submit({preventDefault(){}});
  assert(get('clear-error').textContent.includes('not confirmed'));assert.equal(dispatched.length,1);
  console.log('PASS: active-only escaped buttons, cancel, backend-confirmed success, authorization failure, timeout, token cleanup; no optimistic removal.');
}
run().catch(e=>{console.error(e);process.exitCode=1;});

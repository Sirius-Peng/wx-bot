'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const ROOT=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
let count=0;
function fixture(){
 let doc;
 class Node {
  constructor(){this.listeners={};this.isConnected=true;this.disabled=false;this.open=false;this.visible=true;}
  addEventListener(k,f){(this.listeners[k]??=new Set()).add(f)}
  removeEventListener(k,f){this.listeners[k]?.delete(f)}
  emit(k,e={}){for(const f of [...(this.listeners[k]||[])])f(e)}
  focus(){doc.activeElement=this;this.focused=(this.focused||0)+1}
  getClientRects(){return this.visible?[{}]:[]}
  showModal(){if(this.fail)throw Error('fixture');this.open=true}
  close(){this.open=false;this.emit('close')}
 }
 const nodes={};for(const id of ['action-confirm','action-confirm-cancel','action-confirm-accept','action-confirm-message','input'])nodes[id]=new Node();
 doc=new Node();doc.activeElement=nodes.input;doc.getElementById=id=>nodes[id];
 const c={document:doc,console:{error(){}}};vm.createContext(c);vm.runInContext(fs.readFileSync(path.join(ROOT,'src/web/confirmation.js'),'utf8'),c);
 return {nodes,doc,ask:message=>c.confirmAction(message)};
}
async function tests(){
 // Baseline probe also runs against a rollback copy; it intentionally detects unsafe routes.
 const app=fs.readFileSync(path.join(ROOT,'src/web/app.js'),'utf8');
 assert(!/\b(?:confirm|alert|prompt)\s*\(/.test(app),'blocking native dialog still used');count++;
 const html=fs.readFileSync(path.join(ROOT,'src/web/index.html'),'utf8');
 assert(html.indexOf('/static/confirmation.js')<html.indexOf('/static/app.js'));assert(html.includes('aria-describedby="action-confirm-message"'));count++;
 for(const [event,result] of [['action-confirm-accept',true],['action-confirm-cancel',false]]){
  const f=fixture(),p=f.ask('<img onerror="fixture">\nline2');assert(f.nodes['action-confirm'].open);assert.equal(f.doc.activeElement,f.nodes['action-confirm-cancel']);assert.equal(f.nodes['action-confirm-message'].textContent,'<img onerror="fixture">\nline2');
  f.nodes[event].emit('click');assert.equal(await p,result);assert(!f.nodes['action-confirm'].open);assert.equal(f.doc.activeElement,f.nodes.input);assert.equal(f.doc.listeners.keydown.size,0);count++;
 }
 {
  const f=fixture(),p=f.ask('one');assert.equal(await f.ask('two'),false);f.nodes['action-confirm-accept'].emit('click');assert.equal(await p,true);const p2=f.ask('three');f.nodes['action-confirm-cancel'].emit('click');assert.equal(await p2,false);count++;
 }
 {
  const f=fixture(),p=f.ask('escape');let stopped=0,prevented=0;
  f.doc.emit('keydown',{key:'Escape',isComposing:true});assert(f.nodes['action-confirm'].open);
  f.doc.emit('keydown',{key:'Escape',keyCode:229});assert(f.nodes['action-confirm'].open);
  f.doc.emit('keydown',{key:'Escape',preventDefault(){prevented++},stopImmediatePropagation(){stopped++}});assert.equal(await p,false);assert.equal(stopped,1);assert.equal(prevented,1);count++;
 }
 for(const mutation of ['detached','disabled','hidden']){
  const f=fixture(),p=f.ask('focus');if(mutation==='detached')f.nodes.input.isConnected=false;if(mutation==='disabled')f.nodes.input.disabled=true;if(mutation==='hidden')f.nodes.input.visible=false;
  f.nodes['action-confirm-cancel'].emit('click');await p;assert(!f.nodes.input.focused);count++;
 }
 for(const event of ['close','cancel']){
  const f=fixture(),p=f.ask('native DOM dismissal');if(event==='close')f.nodes['action-confirm'].close();else f.nodes['action-confirm'].emit(event,{preventDefault(){}});assert.equal(await p,false);assert(!f.nodes['action-confirm'].open);count++;
 }
 {
  const f=fixture(),p=f.ask('first');f.nodes['action-confirm-accept'].emit('click');await p;
  const next=f.ask('next');f.nodes['action-confirm'].emit('close');assert(f.nodes['action-confirm'].open);
  f.nodes['action-confirm-accept'].emit('click');assert.equal(await next,true);count++;
 }
 {
  const f=fixture();f.nodes['action-confirm'].fail=true;assert.equal(await f.ask('failed'),false);assert.equal(await f.ask('failed again'),false);assert.equal(f.doc.listeners.keydown.size,0);count++;
 }
 // Window/renderer focus recovery is bounded by visibility, identity and lifecycle.
 const appState={isQuitting:false},mod={exports:{}};
 vm.runInNewContext(fs.readFileSync(path.join(ROOT,'desktop/src/main/window.js'),'utf8'),{module:mod,__dirname:ROOT,require:n=>n==='electron'?{app:appState}:n==='path'?path:{platform:'win32'}});
 const manager=new mod.exports.WindowManager();let focused=0,renderer=0;
 const parent={isDestroyed:()=>false,isVisible:()=>true,isMinimized:()=>false,focus(){focused++},webContents:{isDestroyed:()=>false,focus(){renderer++}}};
 manager.win=parent;manager.restoreInputFocus(parent);assert.equal(focused,1);assert.equal(renderer,1);count++;
 for(const block of ['hidden','minimized','destroyed','rendererDestroyed','quit','otherWindow']){
  const p={...parent,webContents:{...parent.webContents}};
  if(block==='hidden')p.isVisible=()=>false;if(block==='minimized')p.isMinimized=()=>true;if(block==='destroyed')p.isDestroyed=()=>true;if(block==='rendererDestroyed')p.webContents.isDestroyed=()=>true;
  manager.win=p;appState.isQuitting=block==='quit';manager.restoreInputFocus(block==='otherWindow'?parent:p);assert.equal(focused,1);assert.equal(renderer,1);appState.isQuitting=false;count++;
 }
 // File chooser restores on success, cancel and rejection, not on untrusted senders.
 for(const outcome of ['success','cancel','error']){
  const handlers={},mainFrame={url:'http://127.0.0.1:18000/'},sender={mainFrame};let restored=0,shown=0;const m={exports:{}};
  const win={webContents:sender};const mgr={win,restoreInputFocus:p=>{assert.equal(p,win);restored++}};
  vm.runInNewContext(fs.readFileSync(path.join(ROOT,'desktop/src/main/ipc.js'),'utf8'),{module:m,URL,require:n=>n==='electron'?{ipcMain:{handle:(n,h)=>handlers[n]=h},dialog:{showOpenDialog:async()=>{shown++;if(outcome==='error')throw Error('fixture');return {canceled:outcome==='cancel',filePaths:['C:\\fixture']}}}}:{}});
  m.exports.registerIpc({windowMgr:mgr,backend:{url:'http://127.0.0.1:18000'},trayMgr:{},quit(){}});
  const call=()=>handlers['data:chooseLegacyDir']({sender,senderFrame:mainFrame});if(outcome==='error')await assert.rejects(call,/fixture/);else await call();assert.equal(restored,1);
  mainFrame.url='https://external.example/';assert((await call()).canceled);assert.equal(restored,1);assert.equal(shown,1);count++;
 }
 console.log(`INPUT_FOCUS PASS tests=${count} native_confirm=0 accept_cancel=1 ime=1 duplicate_guard=1 bounded_renderer_focus=1 chooser_finally=1`);
}
tests().catch(e=>{console.error(e.message);process.exitCode=1});

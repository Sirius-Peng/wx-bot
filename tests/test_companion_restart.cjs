'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {EventEmitter}=require('node:events');
const ROOT=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
let tests=0;
async function test(name,fn){await fn();tests++;}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function shell(){
 const state={menu:null,tray:null,windows:[],companions:[],backends:[]};
 const app=new EventEmitter();Object.assign(app,{requestSingleInstanceLock:()=>true,whenReady:async()=>{},quit(){}});
 class Backend extends EventEmitter{
  constructor(){super();this.port=17800;this.next={ok:true,port:17801};state.backends.push(this);}
  get url(){return `http://127.0.0.1:${this.port}`;}
  async start(){return {ok:true,port:this.port};}
  async restart(){if(this.next.ok)this.port=this.next.port;return this.next;}
 }
 class WindowManager{
  constructor(){this.reloads=[];state.windows.push(this);}
  create(){}reload(url){this.reloads.push(url);}show(){}
 }
 class CompanionWindow{
  constructor(){this.reloads=[];state.companions.push(this);}
  reload(url){this.reloads.push(url);}show(){}
 }
 class TrayManager{create(value){state.tray=value;}setStatus(){}notify(){}}
 const modules={electron:{app,Menu:{buildFromTemplate:t=>(state.menu=t),setApplicationMenu(){}},dialog:{showErrorBox(){}},nativeImage:{}},
  './paths':{platform:'win32'},'./backend':{Backend},'./window':{WindowManager},'./tray':{TrayManager},
  './ipc':{registerIpc(){}},'./companion':{CompanionWindow},'./lifecycle':{installQuitGuard(){},errorPage:()=>''}};
 vm.runInNewContext(fs.readFileSync(path.join(ROOT,'desktop/src/main/index.js'),'utf8'),{
  require:name=>modules[name]||require(name),process:{env:{},on(){},exit(){}},console,__dirname:path.join(ROOT,'desktop/src/main')
 });
 return state;
}
(async()=>{
 for(const via of ['menu','tray']){
  for(const ok of [true,false])await test(`${via} manual restart ${ok?'new port':'failure'}`,async()=>{
   const h=shell();await flush();const b=h.backends[0],w=h.windows[0],c=h.companions[0];
   w.reloads.length=0;b.next={ok,port:17999,error:'synthetic restart failure'};
   const action=via==='tray'?h.tray.onRestart:h.menu.find(m=>m.label==='帮助').submenu.find(m=>m.label==='重启后台服务').click;
   await action();assert.deepEqual(w.reloads,ok?['http://127.0.0.1:17999']:[]);
   assert.deepEqual(c.reloads,w.reloads);assert.equal(h.companions.length,1,'reuse the existing companion manager');
  });
 }
 await test('automatic crash restart still synchronizes both surfaces',async()=>{
  const h=shell();await flush();h.windows[0].reloads.length=0;
  h.backends[0].emit('restarted',{ok:true,port:18001});
  assert.deepEqual(h.windows[0].reloads,['http://127.0.0.1:18001']);
  assert.deepEqual(h.companions[0].reloads,['http://127.0.0.1:18001']);
 });
 for(const ok of [true,false])await test(`IPC restart ${ok?'new port':'failure'}`,async()=>{
  const handlers={},reloads=[],companions=[],mod={exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(ROOT,'desktop/src/main/ipc.js'),'utf8'),{
   module:mod,URL,require:n=>n==='electron'?{ipcMain:{handle:(name,fn)=>handlers[name]=fn}}:{}
  });
  const result={ok,port:18999};mod.exports.registerIpc({windowMgr:{reload:url=>reloads.push(url)},
   companionMgr:{reload:url=>companions.push(url)},backend:{restart:async()=>result},trayMgr:{setStatus(){}}});
  assert.equal(await handlers['backend:restart'](),result);
  assert.deepEqual(reloads,ok?['http://127.0.0.1:18999']:[]);assert.deepEqual(companions,reloads);
 });
 await test('companion reload changes URL without creating another native window',()=>{
  const module={exports:{}},native=[];
  class BrowserWindow{
   constructor(){this.urls=[];this.webContents={setWindowOpenHandler(){},on(){}};native.push(this);}
   loadURL(url){this.urls.push(url);}once(){}on(){}isDestroyed(){return false;}
   setIgnoreMouseEvents(){}show(){}focus(){}
  }
  vm.runInNewContext(fs.readFileSync(path.join(ROOT,'desktop/src/main/companion.js'),'utf8'),{
   module,__dirname:path.join(ROOT,'desktop/src/main'),URL,clearTimeout,setTimeout,
   require:n=>n==='electron'?{BrowserWindow,app:{},screen:{getDisplayMatching:()=>({workArea:{x:0,y:0,width:1920,height:1080}})}}:
    n==='./paths'?{userDataDir:()=>'/synthetic-unwritten-companion-path'}:require(n)
  });
  const c=new module.exports.CompanionWindow();c.show('http://127.0.0.1:17800');
  c.reload('http://127.0.0.1:17999');c.show('http://127.0.0.1:17999');
  assert.equal(native.length,1);assert.deepEqual(native[0].urls,['http://127.0.0.1:17800/companion','http://127.0.0.1:17999/companion']);
 });
 console.log(`COMPANION_RESTART PASS tests=${tests} manual_menu=1 manual_tray=1 manual_ipc=1 failure_preserved=1 automatic=1 same_window=1`);
})().catch(error=>{console.error(error);process.exitCode=1;});

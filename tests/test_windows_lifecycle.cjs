'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert'),{EventEmitter}=require('events');
const ROOT=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(ROOT,'desktop/src/main/backend.js'),'utf8');
const flush=()=>new Promise(r=>setImmediate(r));
function harness({ready=true,failSpawn=false,timeout=false,port='17800'}={}) {
 const children=[],timers=[],kills=[],logs=[];let tick=0;
 const net={createServer(){const s=new EventEmitter();s.unref=()=>{};s.listen=(p,h,cb)=>{s.port=p||19876;cb();};s.address=()=>({port:s.port});s.close=cb=>cb();return s;}};
 const http={get(opts,cb){const q=new EventEmitter();q.destroy=()=>{};setImmediate(()=>{if(ready){cb({statusCode:200,resume(){}});}else{tick+=timeout?50000:1;q.emit('error',new Error('not-ready'));}});return q;}};
 const spawn=()=>{const p=new EventEmitter();p.pid=8000+children.length;p.stdout=new EventEmitter();p.stderr=new EventEmitter();p.kill=()=>{kills.push(p.pid);p.emit('exit',0,'SIGTERM');};children.push(p);if(failSpawn)setImmediate(()=>p.emit('error',new Error('ENOENT')));return p;};
 const childProcess={spawn,spawnSync(cmd,args){kills.push(Number(args[1]));const c=children.find(c=>c.pid===Number(args[1]));c?.emit('exit',0,null);return {status:0};}};
 const module={exports:{}};const paths={resolveBackend:()=>({kind:'binary',command:'C:\\中文 空格\\wxbot-server.exe',args:[]}),userDataDir:()=>'/tmp/mockdata',logDir:()=>'/tmp/mockdata/logs',legacyDataDirs:()=>[]};
 vm.runInNewContext(source,{__dirname:path.join(ROOT,'desktop/src/main'),require:n=>n==='./paths'?paths:n==='net'?net:n==='http'?http:n==='child_process'?childProcess:n==='fs'?{createWriteStream:()=>({write:s=>logs.push(s)})}:require(n),module,process:{platform:'win32',cwd:()=>'/tmp',env:{WXBOT_PREFERRED_PORT:port}},Date:class extends Date {static now(){return tick;}},setTimeout(fn,ms){if(ms>=2000){const t={fn,ms,cancelled:false};timers.push(t);return t;}return setTimeout(fn,0);},clearTimeout(t){if(t&&typeof t==='object'&&'cancelled'in t)t.cancelled=true;else clearTimeout(t);}}, {filename:'backend.js'});
 const b=new module.exports.Backend();b.on('error',()=>{});b._requestShutdown=async()=>{const p=b.proc;p?.emit('exit',0,null);return true;};
 return {b,children,timers,kills,logs,exports:module.exports};
}
let tests=0;async function test(name,fn){await fn();tests++;}
(async()=>{
 await test('concurrent starts share readiness',async()=>{const h=harness();const a=h.b.start(),b=h.b.start();assert.strictEqual(a,b);assert.strictEqual(h.b.state,'starting');assert.strictEqual((await a).ok,true, h.logs.join('\n'));assert.strictEqual(h.children.length,1);await h.b.stop();});
 await test('stopping before port resolution cancels spawn',async()=>{const h=harness();const p=h.b.start();await h.b.stop();assert.strictEqual((await p).ok,false);assert.strictEqual(h.children.length,0);});
 await test('stop while health probe pending never emits ready',async()=>{const h=harness();let ready=0;h.b.on('ready',()=>ready++);const p=h.b.start();await Promise.resolve();await Promise.resolve();await h.b.stop();assert.strictEqual((await p).ok,false);assert.strictEqual(ready,0);});
 await test('spawn failure exits readiness promptly',async()=>{const h=harness({ready:false,failSpawn:true});const r=await h.b.start();assert.strictEqual(r.ok,false);assert.strictEqual(h.b.proc,null);assert.strictEqual(h.b.state,'crashed');});
 await test('startup timeout cleans its child',async()=>{const h=harness({ready:false,timeout:true});const r=await h.b.start();assert.strictEqual(r.ok,false);assert.strictEqual(h.b.proc,null);assert.strictEqual(h.children.length,1);assert.strictEqual(h.b._stopping,true);});
 await test('stale child exit cannot clear replacement',async()=>{const h=harness();await h.b.start();const old=h.children[0];const replacement={pid:9999};h.b.proc=replacement;old.emit('exit',1,null);assert.strictEqual(h.b.proc,replacement);h.b.proc=null;await h.b.stop();});
 await test('stop cancels pending crash restart',async()=>{const h=harness();await h.b.start();h.children[0].emit('exit',1,null);assert(h.b._restartTimer);await h.b.stop();const t=h.timers.at(-1);assert.strictEqual(t.cancelled,true);t.fn();await flush();assert.strictEqual(h.children.length,1);});
 await test('parallel restarts coalesce',async()=>{const h=harness();await h.b.start();const a=h.b.restart(),b=h.b.restart();assert.strictEqual(a,b);assert.strictEqual((await a).ok,true);assert.strictEqual(h.children.length,2);await h.b.stop();});
 await test('parallel stop calls coalesce',async()=>{const h=harness();await h.b.start();const a=h.b.stop(),b=h.b.stop();assert.strictEqual(a,b);await a;assert.strictEqual(h.b.state,'stopped');});
 await test('invalid preferred port defaults',async()=>{assert.strictEqual(harness({port:'bad'}).exports.PREFERRED_PORT,17800);assert.strictEqual(harness({port:'70000'}).exports.PREFERRED_PORT,17800);assert.strictEqual(harness({port:'18010'}).exports.PREFERRED_PORT,18010);});
 await test('quit during restart delay prevents resurrection',async()=>{const h=harness();await h.b.start();const p=h.b.restart();await Promise.resolve();await Promise.resolve();await h.b.stop();assert.strictEqual((await p).ok,false);assert.strictEqual(h.children.length,1);});
 const {installQuitGuard,errorPage,escapeHtml}=require(path.join(ROOT,'desktop/src/main/lifecycle.js'));
 await test('native quit is intercepted until cleanup',async()=>{const app=new EventEmitter();let prevented=0,quit=0;installQuitGuard(app,()=>{quit++;app.isQuitting=true;});app.emit('before-quit',{preventDefault:()=>prevented++});assert.strictEqual(prevented,1);assert.strictEqual(quit,1);app.emit('before-quit',{preventDefault:()=>prevented++});assert.strictEqual(prevented,1);});
 await test('error page escapes filename/error markup',async()=>{assert.strictEqual(escapeHtml('<script>&"'), '&lt;script&gt;&amp;&quot;');const html=decodeURIComponent(errorPage('<img src=x onerror=evil()>','<b>oops</b>').split(',').slice(1).join(','));assert(!html.includes('<img'));assert(html.includes('&lt;b&gt;'));});
 await test('retry actually restarts backend, not reload error page',async()=>{const html=decodeURIComponent(errorPage('failed','details'));assert(html.includes('window.wxbotDesktop.backend.restart()'));assert(!html.includes('location.reload()'));});
 await test('Windows dev command avoids POSIX env assignment',async()=>{const pkg=JSON.parse(fs.readFileSync(path.join(ROOT,'desktop/package.json')));assert.strictEqual(pkg.scripts.dev,'node scripts/dev.cjs');const script=fs.readFileSync(path.join(ROOT,'desktop/scripts/dev.cjs'),'utf8');assert(script.includes("WXBOT_DEV:'1'"));});
 console.log(`WINDOWS_LIFECYCLE PASS tests=${tests} start_coalesced=1 cancelled_start=1 timeout_cleanup=1 stale_exit_ignored=1 quit_guard=1 retry_real_restart=1`);
})().catch(e=>{console.error(e);process.exitCode=1;});

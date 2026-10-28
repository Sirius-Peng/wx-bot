'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.env.WXBOT_TEST_ROOT || path.resolve(__dirname, '..');
const fullApp = fs.readFileSync(path.join(root, 'src/web/app.js'), 'utf8');
const app = fullApp.split('init().catch')[0];
const guide = fs.readFileSync(path.join(root, 'src/web/guide.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'src/web/index.html'), 'utf8');
function fixture() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {id, hidden: true, disabled: false, value: '', textContent: '', innerHTML: '', checked: false, children: [], style: {}, dataset: {}, listeners: {},
      classList: {toggle() {}, add() {}, remove() {}}, addEventListener(name, fn) {this.listeners[name] = fn;}, setAttribute() {}, removeAttribute() {}, focus() {}, closest() {return null;}, replaceChildren(...items) {this.children = items;}});
    return nodes.get(id);
  }
  const context = {console, location: {hostname: '127.0.0.1', port: '8000', hash: ''}, history: {replaceState() {}}, navigator: {}, window: {addEventListener() {}},
    document: {querySelector: node, querySelectorAll() {return [];}, addEventListener() {}, createElement() {return {set textContent(v) {this.innerHTML = String(v);}};}},
    setInterval(fn) {context.loginPoll = fn; return 1;}, clearInterval() {}, setTimeout, Promise, Date, Image: class {decode() {return Promise.resolve();}}};
  vm.createContext(context); vm.runInContext(guide + '\n' + app, context);
  vm.runInContext(`STATE.config={adapter:{qq_enabled:true,napcat_mode:'native',napcat_webui_token:''},llm:{},bot:{}};loadConfig=async()=>{};refreshOnboarding=async()=>({});refreshStatus=async()=>{};toast=()=>{};`, context);
  return {context, node, run: code => vm.runInContext(code, context)};
}
(async () => {
  let tests = 0;
  // No Docker probe/install request is part of native start, even if no engine exists.
  {
    const f = fixture(); f.context.calls = []; f.context.done = 0;
    f.run(`refreshQQStatus=async()=>({manager:{mode:'native',supported:true,installed:false,running:false},napcat:{running:false},task:{}});pollQQStatus=async()=>({manager:{mode:'native',running:true},task:{},loggedIn:true});finishQQLogin=async()=>done++;api=async(path)=>{calls.push(path);return {ok:true}};`);
    assert.equal(await f.run('setupQQ({saveForm:false})'), true);
    assert.deepEqual(Array.from(f.context.calls), ['/api/qq/setup']); assert.equal(f.context.done, 1); tests++;
  }
  // A responding HTTP endpoint does not turn native mode into an inferred external gateway.
  {
    const f = fixture(); f.context.calls = []; f.context.logins = 0;
    f.run(`refreshQQStatus=async()=>({manager:{mode:'native',supported:true,running:false},httpReachable:true,napcat:{running:false},task:{}});pollQQStatus=async()=>({manager:{mode:'native',running:true},task:{stage:'waiting_login'}});beginQQLogin=async()=>logins++;api=async(path)=>{calls.push(path);return {ok:true}};`);
    assert.equal(await f.run('setupQQ({saveForm:false})'), true); assert.deepEqual(Array.from(f.context.calls), ['/api/qq/setup']); assert.equal(f.context.logins, 1); tests++;
  }
  // Explicit external mode never launches, installs, or manages that process.
  for (const online of [true, false]) {
    const f = fixture(); f.context.calls = [];
    f.context.online = online;
    f.run(`refreshQQStatus=async()=>({manager:{mode:'external',running:true},napcat:{running:true},httpReachable:online,loggedIn:online,task:{}});api=async(path)=>{calls.push(path);return {ok:true}};`);
    assert.equal(await f.run('setupQQ({saveForm:false})'), online); assert.equal(f.context.calls.length, 0); tests++;
  }
  // macOS/unsupported architecture tells the user to use external OneBot, without a Docker fallback.
  {
    const f = fixture(); f.context.calls = [];
    f.run(`refreshQQStatus=async()=>({manager:{mode:'native',supported:false,message:'内置网关仅支持 Windows x64，请选择外部 OneBot'},task:{}});api=async(path)=>calls.push(path);`);
    assert.equal(await f.run('setupQQ({saveForm:false})'), false); assert.equal(f.context.calls.length, 0);
    assert.match(f.node('#home-qq-status').textContent, /Windows x64/); assert.equal(f.node('#btn-home-qq').disabled, false); tests++;
  }
  // Failed startup never opens an account/QR dialog or leaves the one-click button disabled.
  {
    const f = fixture(); f.context.logins = 0;
    f.run(`refreshQQStatus=async()=>({manager:{mode:'native',supported:true},task:{}});api=async()=>({ok:false,message:'fixture failure'});beginQQLogin=async()=>logins++;`);
    assert.equal(await f.run('setupQQ({saveForm:false})'), false); assert.equal(f.context.logins, 0); assert.equal(f.node('#btn-home-qq').disabled, false); tests++;
  }
  // Checklist trusts the manager's mode, not HTTP reachability or container state.
  {
    const f = fixture(); f.context.checks = [];
    f.run(`setCheck=(...args)=>checks.push(args);renderQQChecklist({manager:{mode:'native',supported:true,installed:true,running:false},httpReachable:true,napcat:{running:false},task:{}});`);
    const checks = Array.from(f.context.checks); assert.equal(checks[0][0], 'qq-runtime'); assert.match(checks[0][2], /内置/); assert.doesNotMatch(checks[1][2], /外部/); tests++;
  }
  {
    const f = fixture(); f.context.checks = [];
    f.run(`setCheck=(...args)=>checks.push(args);renderQQChecklist({manager:{mode:'external'},httpReachable:false,napcat:{running:false},task:{}});`);
    assert.notEqual(f.context.checks[1][1], 'ok'); assert.match(f.context.checks[1][2], /等待外部/); tests++;
  }
  // Changing a presentation draft hides managed login controls without saving other cards.
  {
    const f = fixture(); f.context.closed = 0; f.run('closeQQDialogs=()=>closed++');
    f.node('#napcat-mode').value = 'external'; f.run('syncQQMode(true)'); assert.equal(f.node('#qq-native-login').hidden, true); assert.equal(f.node('#qq-external-settings').open, true); assert.equal(f.context.closed, 1);
    f.node('#napcat-mode').value = 'native'; f.run('syncQQMode()'); assert.equal(f.node('#qq-native-login').hidden, false); assert.match(f.node('#qq-mode-hint').textContent, /Windows x64/); tests++;
  }
  // Channel collection preserves the inert legacy mirror, but submits mode independently of model/persona drafts.
  {
    const f = fixture(); f.run(`STATE.config.adapter.napcat_image_mirror='legacy.example';`); f.node('#napcat-mode').value = 'external';
    const config = f.run('collectConfig()'); assert.equal(config.adapter.napcat_mode, 'external'); assert.equal(config.adapter.napcat_image_mirror, 'legacy.example');
    const fields = Array.from(f.run('SECTION_FIELDS.channels[1]')); assert(fields.includes('napcat_mode')); assert(!fields.includes('napcat_image_mirror')); assert(!fields.includes('persona')); tests++;
  }
  // Execute channel saving: only mode changes, unrelated model drafts and old mirror stay untouched.
  {
    const f = fixture(); f.context.savedCalls = [];
    f.run(`STATE.config.adapter={wechat_enabled:true,qq_enabled:true,wechat_type:'ilink',napcat_mode:'native',onebot_http_url:'http://127.0.0.1:3000',onebot_token:'saved-token',bot_qq:'12345',bot_nickname:'bot',napcat_image_mirror:'legacy.example'};STATE.config.llm.primary_key='saved-key';CHANNELS.qq=true;api=async(path,opts)=>{savedCalls.push([path,JSON.parse(opts.body)]);return {config:{...STATE.config,adapter:{...STATE.config.adapter,napcat_mode:'external'}}}};`);
    for (const [id, value] of [['napcat-mode','external'],['onebot-http','http://127.0.0.1:3000'],['onebot-token','saved-token'],['bot-qq','12345'],['qq-nickname','bot'],['primary-key','UNSAVED-MODEL-DRAFT']]) f.node('#'+id).value = value;
    assert.equal(await f.run("saveSection('channels', true)"), true);
    assert.equal(f.context.savedCalls[0][0], '/api/config/section/channels');
    assert.deepEqual(JSON.parse(JSON.stringify(f.context.savedCalls[0][1])), {napcat_mode:'external'});
    assert.equal(f.node('#primary-key').value, 'UNSAVED-MODEL-DRAFT'); assert.equal(f.run('STATE.config.adapter.napcat_image_mirror'), 'legacy.example'); tests++;
  }
  // Homepage and first-run guide state clearly disclose unsupported bundled mode.
  {
    const f = fixture(); f.run(`renderGuide=()=>{};renderHomeQQ({manager:{mode:'native',supported:false,message:'内置网关仅支持 Windows x64，使用外部 OneBot'},task:{}});`);
    assert.match(f.node('#home-qq-status').textContent, /外部 OneBot/); assert.match(f.node('#guide-qq-status').textContent, /Windows x64/); tests++;
  }
  // Completing an external login must not reconfigure a saved old managed-WebUI token.
  {
    const f = fixture(); f.context.synced = 0; f.run(`STATE.config.adapter.napcat_mode='external';STATE.config.adapter.napcat_webui_token='legacy-token';syncQQWsUrl=async()=>synced++;refreshQQStatus=async()=>({});`);
    await f.run('finishQQLogin()'); assert.equal(f.context.synced, 0); tests++;
  }
  // Execute the actual mode change binding rather than only checking its source spelling.
  {
    const f = fixture(); const start = fullApp.indexOf("  $('#napcat-mode').addEventListener('change'"); const end = fullApp.indexOf('\n', start);
    assert(start > 0); f.context.changed = 0; f.run('syncQQMode=(changed)=>{if(changed)globalThis.changed++}'); f.run(fullApp.slice(start, end));
    f.node('#napcat-mode').listeners.change(); assert.equal(f.context.changed, 1); tests++;
  }
  // Stop controls are scoped to owned native processes or the native starting task only.
  {
    const f = fixture(); f.node('#napcat-mode').value='native';
    for (const [st, hidden, disabled] of [
      [{manager:{mode:'native',owned:true,running:true}},false,false],
      [{manager:{mode:'native',owned:false,running:true}},false,true],
      [{manager:{mode:'native',owned:false,running:false},task:{running:true,stage:'starting'}},false,false],
      [{manager:{mode:'native',owned:false,running:false},task:{running:true,stage:'extract'}},false,true],
      [{manager:{mode:'external',owned:true,running:true}},true,true]
    ]) {f.context.st=st;f.run('renderQQStopButton(st)');assert.equal(f.node('#btn-qq-stop-native').hidden,hidden);assert.equal(f.node('#btn-qq-stop-native').disabled,disabled);}
    tests++;
  }
  // Repeated stop clicks share one request; start cannot race the pending stop.
  {
    const f = fixture(); f.context.calls=[];
    f.run(`STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};api=async(path)=>{calls.push(path);return await new Promise(resolve=>stopReady=()=>resolve({ok:true}))};refreshQQStatus=async()=>STATE.qqStatus={manager:{mode:'native',owned:false,running:false}};`);
    const first=f.run('stopNativeQQ()'),second=f.run('stopNativeQQ()');
    assert.deepEqual(Array.from(f.context.calls),['/api/qq/stop']);assert.equal(await f.run('setupQQ({saveForm:false})'),false);assert.equal(await f.run('getQQQr()'),false);assert.equal(await f.run("quickLoginQQ({qq:'12345'})"),false);assert.equal(await f.run('beginQQLogin()'),false);assert.equal(f.context.calls.length,1);assert.equal(f.node('#btn-home-qq').disabled,true);
    f.context.stopReady();assert.equal(await first,true);assert.equal(await second,true);
    assert.equal(f.node('#btn-qq-stop-native').disabled,true);assert.equal(f.node('#btn-home-qq').disabled,false);assert.match(f.node('#home-qq-status').textContent,/账号资料保留.*不会自动恢复/);tests++;
  }
  // Explicit external and unowned native processes are never sent a stop request.
  for (const mode of ['external','native']) {
    const f = fixture();f.context.calls=[];f.context.mode=mode;
    f.run(`STATE.config.adapter.napcat_mode=mode;STATE.qqStatus={manager:{mode,owned:false,running:true}};api=async(path)=>calls.push(path);`);
    assert.equal(await f.run('stopNativeQQ()'),false);assert.equal(f.context.calls.length,0);tests++;
  }
  // A QR arriving after stop cannot open a new modal.
  {
    const f = fixture();f.run(`STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};api=async(path)=>path.endsWith('/qrcode')?await new Promise(resolve=>qrReady=()=>resolve({ok:true,image:'data:fixture'})):{ok:true};refreshQQStatus=async()=>STATE.qqStatus={manager:{mode:'native',owned:false,running:false}};`);
    const qr=f.run('getQQQr()');assert.equal(await f.run('stopNativeQQ()'),true);f.context.qrReady();assert.equal(await qr,false);assert.equal(f.node('#qq-login-modal').hidden,true);tests++;
  }
  // Stop also invalidates an already returned QR while image decode is pending.
  {
    const f = fixture();f.context.Image=class{decode(){return new Promise(resolve=>f.context.decodeReady=resolve)}};
    f.run(`STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};api=async(path)=>path.endsWith('/qrcode')?{ok:true,image:'data:fixture'}:{ok:true};refreshQQStatus=async()=>STATE.qqStatus={manager:{mode:'native',owned:false,running:false}};`);
    const qr=f.run('getQQQr()');await new Promise(resolve=>setImmediate(resolve));assert(f.context.decodeReady);assert.equal(await f.run('stopNativeQQ()'),true);f.context.decodeReady();assert.equal(await qr,false);assert.equal(f.node('#qq-login-modal').hidden,true);tests++;
  }
  // A successful old startup waiting to reload config cannot open login after stop.
  {
    const f = fixture();f.context.logins=0;f.context.stopped=false;
    f.run(`STATE.qqStatus={manager:{mode:'native',supported:true,owned:true,running:true},task:{},loggedIn:false};refreshQQStatus=async()=>STATE.qqStatus=stopped?{manager:{mode:'native',owned:false,running:false}}:STATE.qqStatus;pollQQStatus=refreshQQStatus;api=async(path)=>{if(path.endsWith('/stop'))stopped=true;return {ok:true}};loadConfig=async()=>await new Promise(resolve=>loadReady=resolve);beginQQLogin=async()=>logins++;`);
    const start=f.run('setupQQ({saveForm:false})');await new Promise(resolve=>setImmediate(resolve));assert(f.context.loadReady);assert.equal(await f.run('stopNativeQQ()'),true);f.context.loadReady();assert.equal(await start,false);assert.equal(f.context.logins,0);tests++;
  }
  // Saved-account and quick-login requests already in flight do not reopen login after stop.
  {
    const f = fixture();f.run(`STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};loadQQAccounts=async()=>await new Promise(resolve=>accountsReady=()=>resolve([{qq:'12345'}]));api=async()=>({ok:true});refreshQQStatus=async()=>STATE.qqStatus={manager:{mode:'native',owned:false,running:false}};`);
    const login=f.run('beginQQLogin()');await f.run('stopNativeQQ()');f.context.accountsReady();assert.equal(await login,false);assert.equal(f.node('#qq-choice-modal').hidden,true);tests++;
  }
  {
    const f = fixture();f.run(`STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};api=async(path)=>path.endsWith('/quick')?await new Promise(resolve=>quickReady=()=>resolve({ok:true,loggedIn:true})):{ok:true};refreshQQStatus=async()=>STATE.qqStatus={manager:{mode:'native',owned:false,running:false}};`);
    const login=f.run("quickLoginQQ({qq:'12345'})");await f.run('stopNativeQQ()');f.context.quickReady();assert.equal(await login,false);assert.equal(f.run('STATE.qqLoginPending'),false);tests++;
  }
  // In-flight login status confirmation is ignored after stop, not treated as a new login.
  {
    const f = fixture();f.context.done=0;
    f.run(`STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};finishQQLogin=async()=>done++;api=async(path)=>path.endsWith('/check')?await new Promise(resolve=>checkReady=()=>resolve({loggedIn:true})):{ok:true};refreshQQStatus=async()=>STATE.qqStatus={manager:{mode:'native',owned:false,running:false}};startQQLoginPolling();`);
    const pending=f.context.loginPoll();await f.run('stopNativeQQ()');f.context.checkReady();await pending;assert.equal(f.context.done,0);assert.equal(f.run('STATE.qqLoginPending'),false);tests++;
  }
  // Stop errors retain retry controls and never delete or replace saved credentials.
  {
    const f = fixture();f.run(`STATE.config.adapter.napcat_webui_token='keep-token';STATE.qqStatus={manager:{mode:'native',owned:true,running:true}};api=async()=>({ok:false,message:'fixture stop error'});`);
    assert.equal(await f.run('stopNativeQQ()'),false);assert.equal(f.node('#btn-qq-stop-native').disabled,false);assert.equal(f.run('STATE.config.adapter.napcat_webui_token'),'keep-token');assert.match(f.node('#home-qq-status').textContent,/停止未完成/);tests++;
  }
  assert(!app.includes('/api/docker/prepare')); assert(!html.includes('id="napcat-image-mirror"')); assert(!html.includes('第一次安装 Docker 怎么操作')); assert(!html.includes('WSL2'));
  assert(html.includes('id="napcat-mode"')); assert(!html.includes('id="napcat-qq-path"'));
  const homeStart = html.indexOf('id="home-qq-card"');
  const home = html.slice(homeStart, html.indexOf('<div class="card recent-card"', homeStart));
  assert.equal((home.match(/<button\b/g) || []).length, 1); tests++;
  assert.equal(tests, 26);
  console.log(`NATIVE_NAPCAT_FRONTEND PASS tests=${tests} no_docker=1 explicit_external=1 mac_external=1 single_home_button=1 legacy_config_retained=1`);
})().catch(error => {console.error(error); process.exitCode = 1;});

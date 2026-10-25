'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/web/app.js'),'utf8');
const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');
const code=source.slice(source.indexOf('function usageCount('),source.indexOf('async function backupStorage()'));
const primary=html.slice(html.indexOf('data-settings-section="primary"'),html.indexOf('data-settings-section="fallback"'));
const elements=new Map();
function element(){return {disabled:false,_text:'',_html:'',set textContent(v){this._text=String(v);this._html='';},get textContent(){return this._text;},set innerHTML(v){this._html=String(v);this._text='';},get innerHTML(){return this._html;}};}
const escape=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
let calls=0,responder=()=>Promise.resolve({usage:fixture()});
const context={Number,Object,Array,esc:escape,$:selector=>{if(!elements.has(selector))elements.set(selector,element());return elements.get(selector);},api:async url=>{calls++;assert.equal(url,'/api/diagnostics');return await responder();}};
vm.createContext(context);vm.runInContext(code,context);
let tests=0;
function field(sum,known=2,unknown=1){return {observed_sum:sum,known_calls:known,unknown_calls:unknown};}
function fixture(){return {scope:'recent_runtime_events',observed_events:2,requests:3,local_search_cache:4,summary:{attempts:3,
 cache:{hit_ratio:.9,overall_hit_ratio:null,hit_tokens:900,input_tokens:1000,known_calls:2,unknown_calls:1,inconsistent_calls:0},
 fields:{prompt_tokens:field(1000),cache_miss_tokens:field(100),completion_tokens:field(1234),reasoning_tokens:field(100)},
 by_task:{chat:{attempts:2,usage_unknown_calls:0},search:{attempts:1,usage_unknown_calls:1}}}};}
function render(value){context.input=value;return vm.runInContext('renderUsageDiagnostics(input)',context);}
async function main(){
 assert(primary.includes('<summary>用量与缓存诊断</summary>'));
 assert(primary.includes('id="btn-usage-refresh"'));assert(primary.includes('id="usage-diagnostics"'));
 assert(primary.includes('data-save-section="primary"'));assert(primary.includes('data-save-status="primary"'));
 const details=primary.match(/<details[^>]*id="usage-diagnostics-panel"[^>]*>/)[0];assert(!/\bopen\b/.test(details));tests++;
 assert.equal(calls,0);assert.equal((source.match(/\bloadUsageDiagnostics\s*\(/g)||[]).length,1);
 assert(source.includes("$('#btn-usage-refresh').addEventListener('click',loadUsageDiagnostics)"));tests++;
 let out=render(fixture());
 assert(out.includes('90.0%'));assert(out.includes('已知输入的加权缓存命中率'));
 assert(out.includes('<span>总体缓存命中率</span><strong>未知</strong>'));
 assert(out.includes('已知 2 次 · 未知 1 次 · 数据不一致 0 次'));
 assert(out.includes('当前运行近期最多 500 条事件，不是账户历史账单'));
 assert(out.includes('思考属于输出子集，不重复相加'));assert(out.includes('此处不估算价格或费用'));tests++;
 assert(out.includes('1,234'));assert(!out.includes('1,334'));
 assert(out.includes('聊天回复：2 次请求'));assert(out.includes('原生搜索：1 次请求'));
 assert(out.includes('未命中输入 token'));assert(out.includes('思考 token（输出子集）'));tests++;
 let raw=fixture();raw.tool_requests={operations:{search:{attempts:2,results_returned:1,failed:0,cancelled:1,result_unknown:1},fetch:{attempts:1,results_returned:1,failed:0,cancelled:0,result_unknown:0}}};
 let rawOut=render(raw);assert(rawOut.includes('官方 Search：2 次尝试，返回非空结果 1 次'));assert(rawOut.includes('结果未知 1 次'));assert(rawOut.includes('原始联网 API（按次，不计入模型 token 或 KV 命中率）'));assert(rawOut.includes('90.0%'));tests++;
 assert(out.includes('本地搜索结果复用：4 次（不是供应商 KV 命中）'));
 let late=fixture();late.summary.by_task.late_reply={attempts:1,usage_unknown_calls:0};
 assert(render(late).includes('稍后接话：1 次请求'));tests++;
 let value=fixture();value.summary.cache.observed_hit_ratio=.75;out=render(value);assert(out.includes('75.0%'));assert(!out.includes('90.0%'));tests++;
 value=fixture();value.summary.cache.hit_ratio=null;value.summary.cache.overall_hit_ratio=null;
 for(const item of Object.values(value.summary.fields))item.observed_sum=null;
 out=render(value);assert(!out.includes('0.0%'));assert.equal((out.match(/<strong>未知<\/strong>/g)||[]).length,6);tests++;
 value=fixture();value.summary.cache.hit_ratio=0;value.summary.cache.overall_hit_ratio=0;
 for(const item of Object.values(value.summary.fields))item.observed_sum=0;
 out=render(value);assert(out.includes('0.0%'));assert.equal((out.match(/<strong>0<\/strong>/g)||[]).length,4);tests++;
 const malicious='<img src=x onerror=alert(1)>';
 value=fixture();value.summary.by_task={[malicious]:{attempts:1,usage_unknown_calls:1}};
 value.summary.fields.prompt_tokens.observed_sum=malicious;
 out=render(value);assert(!out.includes(malicious));assert(out.includes('&lt;img'));assert(out.includes('<span>已知输入 token</span><strong>未知</strong>'));tests++;
 for(const malformed of [null,{}, {scope:'account_invoice',summary:{}}, {scope:'recent_runtime_events',summary:[]}]){
   assert.throws(()=>render(malformed),/未返回当前运行/);
 }tests++;
 await vm.runInContext('loadUsageDiagnostics()',context);
 assert.equal(calls,1);assert.equal(context.$('#btn-usage-refresh').disabled,false);assert(context.$('#usage-diagnostics').innerHTML.includes('90.0%'));tests++;
 responder=()=>Promise.reject(new Error(malicious));
 await vm.runInContext('loadUsageDiagnostics()',context);
 assert.equal(context.$('#usage-diagnostics').innerHTML,'');assert(context.$('#usage-diagnostics').textContent.startsWith('未获取用量诊断：'));
 assert(context.$('#usage-diagnostics').textContent.includes(malicious));assert.equal(context.$('#btn-usage-refresh').disabled,false);tests++;
 responder=()=>Promise.resolve({});await vm.runInContext('loadUsageDiagnostics()',context);
 assert(context.$('#usage-diagnostics').textContent.includes('未获取'));assert(!context.$('#usage-diagnostics').textContent.includes('0%'));tests++;
 let resolve;responder=()=>new Promise(done=>{resolve=done;});
 const before=calls,pending=vm.runInContext('loadUsageDiagnostics()',context);
 assert.equal(context.$('#btn-usage-refresh').disabled,true);
 await vm.runInContext('loadUsageDiagnostics()',context);assert.equal(calls,before+1);
 resolve({usage:fixture()});await pending;assert.equal(context.$('#btn-usage-refresh').disabled,false);tests++;
 console.log(`USAGE_FRONTEND PASS tests=${tests} manual_only=1 weighted_cache=1 unknown_not_zero=1 escaped=1 independent_save=1`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});

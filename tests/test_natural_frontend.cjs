'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/web/app.js'),'utf8');
const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');
const context={Date,Set,Number,esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function conversationStatusText('),source.indexOf('/* ══════════ 概览：最近动态')),context);
vm.runInContext(source.slice(source.indexOf('function fmtMemoryTime('),source.indexOf('function renderMemoryList(')),context);
let tests=0;
for(const [input,expected] of [[{waiting:1},'等待说完'],[{silent:2},'主动静默'],[{uncertain:1},'投递结果待确认'],[{deferred:1},'稍后再判断'],[{replying:2},'正在回应'],[{},'等待新消息'],[{state_error:true},'读取异常']]){assert(context.conversationStatusText(input).includes(expected));tests++;}
assert(context.memoryEvidenceMarkup({evidence_sources:[{id:'a',episode_id:'e1',observed_at:10,text:'<script>x</script>'},{id:'b',episode_id:'e2',observed_at:20,text:'second'}]}).includes('独立对话 2 次'));tests++;
assert(!context.memoryEvidenceMarkup({evidence_sources:[{episode_id:'e',text:'<img onerror=x>'}]}).includes('<img'));tests++;
assert(context.memoryEvidenceMarkup({evidence_count:3,independent_episodes:2}).includes('用户依据 3 次'));tests++;
assert.equal(context.fmtMemoryTime('invalid'),'未知');tests++;
for(const id of ['reply-mode','late-reply-enabled','memory-forgotten','btn-forgotten-refresh']){assert(html.includes('id="'+id+'"'));tests++;}
assert(source.includes("reply: ['bot', ['chat_mode','reply_mode','late_reply_enabled'"));assert(source.includes("$('#reply-mode').value = c.bot.reply_mode"));assert(source.includes("c.bot.late_reply_enabled = $('#late-reply-enabled').checked"));tests++;
assert(html.includes('data-save-section="reply"'));assert(html.includes('data-save-section="persona"'));assert(html.includes('data-save-section="memory"'));tests++;
assert(html.includes('至少两次独立对话、相隔24小时'));assert(!html.includes('首次、明确要求记住时立即抽取'));assert(html.includes('10秒只内部评估'));tests++;
assert(source.includes('data-pin="${esc(it.id)}"'));assert(source.includes('data-pinned="${it.pinned'));assert(source.includes("body:JSON.stringify({pinned})"));tests++;
assert(source.includes('/api/memory/forgotten/${encodeURIComponent(button.dataset.relearn)}'));assert(source.includes('不会恢复旧记忆'));tests++;
assert(!source.includes('私聊必回，QQ 群里 @ 它才回'));tests++;
const css=fs.readFileSync(path.join(root,'src/web/style.css'),'utf8');
assert(/@media \(max-width: 600px\)\s*\{\s*\.memory-item\s*\{[^}]*display: grid;[^}]*grid-template-columns: minmax\(0, 1fr\)/.test(css));assert(css.includes('.memory-item > .mem-actions { width: 100%; margin-left: 0; justify-content: flex-end; }'));tests++;
assert(html.includes('语义模糊、已经说完整的私聊'));assert(html.includes('总限时3秒'));assert(html.includes('不是每条消息都调用'));assert(html.includes('独立 Key 留空时复用主接口和 Key'));tests++;
assert(html.includes('不加载本地判定模型'));assert(html.includes('deepseek-flash 判断'));assert(html.includes('不更改回复模型'));tests++;
console.log(`natural settings frontend PASS tests=${tests}`);

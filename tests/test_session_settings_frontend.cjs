'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/web/app.js'),'utf8');
const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');
const card=html.slice(html.indexOf('data-settings-section="primary"'),html.indexOf('data-settings-section="fallback"'));
let tests=0;
for(const [id,attributes] of [['context-window-tokens',['type="number"','min="0"','step="1"','value="0"']],
                              ['context-compact-percent',['type="number"','min="10"','max="80"','value="40"']]]){
  const input=card.match(new RegExp('<input[^>]*id="'+id+'"[^>]*>'));
  assert(input,'session input must stay in independently saved primary card');
  for(const attribute of attributes)assert(input[0].includes(attribute));
  tests++;
}
assert(card.includes('data-save-section="primary"'));assert(card.includes('data-save-status="primary"'));tests++;
assert(card.includes('40% 才压缩'));assert(card.includes('1,000,000 token'));assert(card.includes('32,768 token'));
assert(card.includes('缓存命中由供应商决定'));tests++;
assert(card.includes('图片会话按实际识图接口的窗口计算'));tests++;
assert(source.includes("$('#context-window-tokens').value = c.llm.context_window_tokens ?? 0"));
assert(source.includes("$('#context-compact-percent').value = (c.llm.context_compact_ratio ?? .4) * 100"));tests++;
const fields=source.slice(source.indexOf('const SECTION_FIELDS ='),source.indexOf('function sectionStatus('));
const context={JSON,Math,Number,CHANNELS:{wechat:true,qq:false},STATE:{config:{adapter:{},llm:{primary_model:'unchanged'},bot:{name:'fixture'}}}};
const elements=new Map();context.$=selector=>{if(!elements.has(selector))elements.set(selector,{value:'',checked:false});return elements.get(selector);};
vm.createContext(context);vm.runInContext(fields,context);
const primary=JSON.parse(vm.runInContext('JSON.stringify(SECTION_FIELDS.primary)',context));
assert.deepEqual(primary,['llm',['primary_base','primary_model','primary_key','context_window_tokens','context_compact_ratio']]);tests++;
for(const section of ['judge','vision','search','reply','memory']){
  const mapping=JSON.parse(vm.runInContext(`JSON.stringify(SECTION_FIELDS.${section})`,context));
  assert(!mapping[1].includes('context_window_tokens'));assert(!mapping[1].includes('context_compact_ratio'));tests++;
}
vm.runInContext(source.slice(source.indexOf('function collectConfig()'),source.indexOf('async function loadProactive()')),context);
context.$('#context-window-tokens').value='65536';context.$('#context-compact-percent').value='40';
let collected=vm.runInContext('collectConfig()',context);
assert.equal(collected.llm.context_window_tokens,65536);assert.equal(collected.llm.context_compact_ratio,.4);tests++;
context.$('#context-window-tokens').value='0';context.$('#context-compact-percent').value='12.5';
collected=vm.runInContext('collectConfig()',context);
assert.equal(collected.llm.context_window_tokens,0);assert.equal(collected.llm.context_compact_ratio,.125);tests++;
assert.equal(context.STATE.config.llm.primary_model,'unchanged');assert(!Object.hasOwn(context.STATE.config.llm,'context_window_tokens'));tests++;
console.log(`SESSION_SETTINGS_FRONTEND PASS tests=${tests} primary_card=1 percent_conversion=1 auto_zero=1 draft_preserved=1`);

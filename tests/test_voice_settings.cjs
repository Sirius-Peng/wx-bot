'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
const root=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');
const source=fs.readFileSync(path.join(root,'src/web/app.js'),'utf8');
const card=html.slice(html.indexOf('<div class="card" data-settings-section="voice"'),html.indexOf('<div class="settings-panel" id="settings-panel-conversation"'));
const voice={enabled:false,provider:'fish',base_url:'https://api.fish.audio',api_key:'••••••••cdef',model_id:'5b432f747d00419aba7cb12fa50e4af0',fish_model:'s2.1-pro',reference_audio:'',reference_text:'',prompt_language:'zh',text_language:'zh',reply_rate:.25,cooldown_seconds:300,max_chars:160,timeout_seconds:20};
const collect=source.slice(source.indexOf('function collectConfig()'),source.indexOf('async function loadProactive()'));
const fields=source.slice(source.indexOf('const SECTION_FIELDS ='),source.indexOf('function sectionStatus('));
const sync=source.slice(source.indexOf('function syncVoiceProvider('),source.indexOf("let voicePreviewUrl ="));
const preview=source.slice(source.indexOf("let voicePreviewUrl ="),source.indexOf('/** 平台卡片：'));
const save=source.slice(source.indexOf('async function saveSection('),source.indexOf('/* ══════════ 扫码登录'));
function fixture(){
 const elements=new Map(),calls=[],dirty=new Set();
 const el=id=>{if(!elements.has(id))elements.set(id,{value:'',checked:false,hidden:false,disabled:false,textContent:''});return elements.get(id);};
 const $=selector=>el(selector.startsWith('#')?selector.slice(1):selector);
 const context={console,JSON,Math,Number,Object,Set,Error,URL:{createObjectURL:()=>{calls.push('create-url');return 'blob:synthetic';},revokeObjectURL:url=>calls.push(['revoke',url])},
  STATE:{config:{adapter:{},llm:{},bot:{name:'fixture'},voice:{...voice}},status:{}},CHANNELS:{wechat:true,qq:false},DIRTY_SECTIONS:dirty,$,
  markSectionDirty:key=>dirty.add(key),sectionStatus:(...value)=>calls.push(['status',...value]),toast(){},loadMemory:async()=>{},loadConfig:async()=>calls.push('load-config')};
 for(const [id,field] of [['provider','provider'],['base-url','base_url'],['model-id','model_id'],['fish-model','fish_model'],['reference-audio','reference_audio'],['reference-text','reference_text'],['prompt-language','prompt_language'],['text-language','text_language'],['cooldown-seconds','cooldown_seconds'],['max-chars','max_chars'],['timeout-seconds','timeout_seconds']])el('voice-'+id).value=String(voice[field]);
 el('voice-reply-percent').value='25';el('voice-preview-text').value='演出还没有结束。';el('voice-preview-audio').hidden=true;
 context.api=async(route,options)=>{const values=JSON.parse(options.body);calls.push([route,values]);const next={...context.STATE.config,voice:{...context.STATE.config.voice,...values}};delete next.voice.clear_api_key;if(values.clear_api_key)next.voice.api_key='';else if(values.api_key)next.voice.api_key='••••••••'+values.api_key.slice(-4);return {config:next,requires_restart:false};};
 context.fetch=async(route,options)=>{calls.push([route,JSON.parse(options.body)]);return {ok:true,status:200,headers:{get:()=> 'audio/wav'},blob:async()=>({size:120})};};
 vm.createContext(context);vm.runInContext(fields+'\n'+collect+'\n'+sync+'\n'+preview+'\n'+save,context);
 return {context,el,calls,dirty,run:code=>vm.runInContext(code,context)};
}

test('voice has its own model-settings card and independent save button',()=>{
 assert(card);assert(card.includes('data-settings-group="models"'));
 assert.equal((card.match(/data-save-section="voice"/g)||[]).length,1);
 assert(card.includes('data-save-status="voice"'));
 assert(html.indexOf('id="settings-panel-models"')<html.indexOf('data-settings-section="voice"'));
 assert(card.includes('音频附件'));assert(card.includes('文字始终保留'));assert(card.includes('不额外调用 LLM'));
});

test('voice credentials are separate from DeepSeek and paid engine is explicit',()=>{
 assert(card.includes('type="password" id="voice-api-key"'));
 assert(card.includes('不复用 DeepSeek Key'));assert(card.includes('Fish API 单独计费'));
 assert(card.includes('id="voice-fish-model"'));assert(card.includes('value="s2.1-pro"'));
 assert(!card.includes('id="primary-key"'));
});

test('all original HTML IDs remain unique including the added voice controls',()=>{
 const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(new Set(ids).size,ids.length);
 for(const id of ['voice-enabled','voice-provider','voice-base-url','voice-api-key','voice-clear-key','voice-fish-model','voice-model-id','voice-reference-audio','voice-reference-text','voice-prompt-language','voice-text-language','voice-reply-percent','voice-cooldown-seconds','voice-max-chars','voice-timeout-seconds','btn-voice-preview','voice-preview-audio'])assert(ids.includes(id),id);
});

test('voice fields map only to their dedicated config root',()=>{
 const h=fixture(),mapping=JSON.parse(h.run('JSON.stringify(SECTION_FIELDS.voice)'));
 assert.equal(mapping[0],'voice');assert(mapping[1].includes('fish_model'));assert(mapping[1].includes('api_key'));
 for(const section of ['primary','reply','persona','search'])assert(!JSON.parse(h.run(`JSON.stringify(SECTION_FIELDS.${section})`))[1].includes('fish_model'));
});

test('percent and zero values collect without changing saved config or primary key',()=>{
 const h=fixture();h.context.STATE.config.llm.primary_key='chat-kept';h.el('voice-reply-percent').value='0';h.el('voice-cooldown-seconds').value='0';
 const data=h.run('collectConfig()');assert.equal(data.voice.reply_rate,0);assert.equal(data.voice.cooldown_seconds,0);
 assert.equal(h.context.STATE.config.voice.reply_rate,.25);assert.equal(h.context.STATE.config.llm.primary_key,'chat-kept');
});

test('switching provider changes only a known default service URL and visibility',()=>{
 const h=fixture();h.el('voice-provider').value='gpt_sovits';h.run('syncVoiceProvider(true)');
 assert.equal(h.el('voice-base-url').value,'http://127.0.0.1:9880');assert.equal(h.el('voice-fish-key-field').hidden,true);assert.equal(h.el('voice-local-audio-field').hidden,false);
 h.el('voice-provider').value='fish';h.run('syncVoiceProvider(true)');assert.equal(h.el('voice-base-url').value,'https://api.fish.audio');assert.equal(h.el('voice-fish-engine-field').hidden,false);assert.equal(h.el('voice-local-text-field').hidden,true);
 h.el('voice-base-url').value='https://my-speech.example/v1';h.el('voice-provider').value='gpt_sovits';h.run('syncVoiceProvider(true)');assert.equal(h.el('voice-base-url').value,'https://my-speech.example/v1');
});

test('blank API key is omitted so an independent card save preserves it',async()=>{
 const h=fixture();h.el('voice-reply-percent').value='30';assert.equal(await h.run("saveSection('voice',true)"),true);
 const sent=h.calls.find(row=>Array.isArray(row)&&row[0]==='/api/config/section/voice')[1];
 assert.deepEqual(sent,{reply_rate:.3});assert.equal(h.context.STATE.config.voice.api_key,voice.api_key);assert.equal(h.el('[data-save-section="voice"]').disabled,false);
});

test('new API key is sent only to the voice section',async()=>{
 const h=fixture();h.el('voice-api-key').value='new-private-key';await h.run("saveSection('voice',true)");
 const sent=h.calls.find(row=>Array.isArray(row)&&row[0]==='/api/config/section/voice')[1];assert.equal(sent.api_key,'new-private-key');assert.deepEqual(Object.keys(sent),['api_key']);
});

test('explicit key clear is a one-shot pseudo field and does not leave a false dirty state',async()=>{
 const h=fixture();h.dirty.add('voice');h.el('voice-clear-key').checked=true;await h.run("saveSection('voice',true)");
 const sent=h.calls.find(row=>Array.isArray(row)&&row[0]==='/api/config/section/voice')[1];assert.deepEqual(sent,{clear_api_key:true});assert.equal(h.context.STATE.config.voice.api_key,'');assert.equal(h.dirty.has('voice'),false);
});

test('failed save retains draft and re-enables the independent button',async()=>{
 const h=fixture();h.context.api=async()=>{throw Error('fixture failed');};h.el('voice-api-key').value='keep-draft';
 assert.equal(await h.run("saveSection('voice',true)"),false);assert.equal(h.el('voice-api-key').value,'keep-draft');assert.equal(h.dirty.has('voice'),true);assert.equal(h.el('[data-save-section="voice"]').disabled,false);
});

test('typing preview text is not a configuration edit',()=>{
 const start=source.indexOf("  $$('[data-settings-section]').forEach(card=>{");const end=source.indexOf("  $('#search-backend')",start);
 assert(start>0&&end>start);const handlers={},dirty=[];
 const card={dataset:{settingsSection:'voice'},addEventListener:(name,fn)=>handlers[name]=fn};
 vm.runInNewContext(source.slice(start,end),{$$:()=>[card],markSectionDirty:name=>dirty.push(name)});
 handlers.input({target:{id:'voice-preview-text',matches:()=>true}});assert.deepEqual(dirty,[]);
 handlers.change({target:{id:'voice-enabled',matches:()=>true}});assert.deepEqual(dirty,['voice']);
});

test('preview uses saved config, bounded user text and blob audio without sending WeChat',async()=>{
 const h=fixture();await h.run('previewVoice()');
 assert.deepEqual(h.calls.find(row=>Array.isArray(row)&&row[0]==='/api/voice/preview')[1],{text:'演出还没有结束。'});
 assert.equal(h.el('voice-preview-audio').src,'blob:synthetic');assert.equal(h.el('voice-preview-audio').hidden,false);assert.equal(h.el('btn-voice-preview').disabled,false);
 assert(h.el('voice-preview-status').textContent.includes('不发送微信'));
 await h.run('previewVoice()');assert(h.calls.some(row=>Array.isArray(row)&&row[0]==='revoke'&&row[1]==='blob:synthetic'));
});

test('preview never invokes paid API for empty, oversized or unsaved configuration',async()=>{
 for(const value of ['', '字'.repeat(161)]){const h=fixture();h.el('voice-preview-text').value=value;await h.run('previewVoice()');assert.equal(h.calls.length,0);}
 const h=fixture();h.dirty.add('voice');await h.run('previewVoice()');assert.equal(h.calls.length,0);assert(h.el('voice-preview-status').textContent.includes('先保存'));
});

test('preview rejects expired session, JSON masquerading as audio and empty audio',async()=>{
 for(const response of [{ok:false,status:401}, {ok:true,status:200,headers:{get:()=> 'application/json'}}, {ok:true,status:200,headers:{get:()=> 'audio/wav'},blob:async()=>({size:0})}]){
  const h=fixture();h.context.fetch=async()=>response;await h.run('previewVoice()');assert.equal(h.el('voice-preview-audio').hidden,true);assert(!h.calls.includes('create-url'));assert(h.el('voice-preview-status').textContent.includes('未完成'));assert.equal(h.el('btn-voice-preview').disabled,false);
 }
});

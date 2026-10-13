'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=process.env.WXBOT_TEST_ROOT||path.resolve(__dirname,'..');
const source=fs.readFileSync(path.join(root,'src/web/statistics.js'),'utf8');
const {packBubbles,bubbleLabelLines}=require(path.join(root,'src/web/statistics.js'));
let tests=0;
assert.deepEqual(bubbleLabelLines('python',35,11),['python']);tests++;
const clipped=bubbleLabelLines('这是一个需要截断的很长中文关键词',22,11);assert.equal(clipped.length,2);assert(clipped[1].endsWith('…'));tests++;
assert.deepEqual(packBubbles([],600,420),[]);tests++;
assert.equal(packBubbles([{count:0},{count:-1},{count:NaN}],600,420).length,0);tests++;
for(const [width,height] of [[280,360],[600,420],[900,440]]) {
 const items=Array.from({length:50},(_,i)=>({label:`word${i}`,count:50-i}));
 const result=packBubbles(items,width,height);assert.equal(result.length,50);
 result.forEach((c,i)=>{assert(c.x-c.r>=0&&c.x+c.r<=width&&c.y-c.r>=0&&c.y+c.r<=height);result.slice(i+1).forEach(o=>assert((c.x-o.x)**2+(c.y-o.y)**2>=(c.r+o.r)**2));});
 assert(Math.abs(result[0].r**2/result[49].r**2-50)<1e-8);tests++;
}
const same=[{label:'x',count:9},{label:'y',count:1}];assert.deepEqual(packBubbles(same,600,420),packBubbles(same,600,420));tests++;
assert.equal(packBubbles(Array.from({length:90},()=>({count:1})),600,420).length,50);tests++;
class Node {
 constructor(tag='div'){this.tag=tag;this.children=[];this.attributes={};this.dataset={};this.style={};this.listeners={};this.textContent='';this.clientWidth=600;this.disabled=false;this.value='';this.classList={toggle(){},contains:()=>true};}
 setAttribute(k,v){this.attributes[k]=v;if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]=v;}
 appendChild(n){this.children.push(n);return n;}append(...n){this.children.push(...n);}replaceChildren(...n){this.children=n;}
 addEventListener(k,fn){this.listeners[k]=fn;}
}
function fixture(){
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
 const context={console,AbortController,URLSearchParams,document:{querySelector:node,querySelectorAll:()=>[],createElement:t=>new Node(t),createElementNS:(_,t)=>new Node(t)},module:{exports:{}},api:async()=>({})};vm.createContext(context);vm.runInContext(source,context);
 return {context,node,run:s=>vm.runInContext(s,context)};
}
const data={words:[{label:'音乐',count:4,messages:2},{label:'咖啡',count:2,messages:2}],topics:[{label:'音乐与艺术',count:2,messages:2,share:100,keywords:['音乐']}],summary:{messages:2,text_messages:2,token_count:6,unique_words:2,active_days:1,unknown_platform_messages:0},filters:{timezone:'Asia/Shanghai'},coverage:{},generated_at:'2026-10-13T21:00:00+08:00'};
async function main(){
 // Visible metrics, SVG button accessibility, exact counts and keyboard selection.
 {const f=fixture();f.context.payload=data;f.run('STATISTICS.data=payload;renderStatistics()');assert.equal(f.node('#stats-messages').textContent,'2');const svg=f.node('#stats-bubbles').children[0];assert.equal(svg.tag,'svg');assert.equal(svg.children.length,2);assert.equal(svg.children[0].attributes.role,'button');svg.children[0].listeners.keydown({key:'Enter',preventDefault(){}});assert(f.node('#stats-selection').textContent.includes('出现 4 次'));tests++;}
 // Labels use text nodes, never innerHTML; a malicious label is inert.
 {const f=fixture();f.context.payload=structuredClone(data);f.context.payload.words[0].label='<img src=x onerror=bad()>';f.run('STATISTICS.data=payload;renderStatistics()');const row=f.node('#stats-ranking').children[0];assert.equal(row.children[1].children[0].textContent,'<img src=x onerror=bad()>');assert.equal(row.children[1].children[0].children.length,0);tests++;}
 {const f=fixture();f.context.payload=data;f.run("STATISTICS.data=payload;STATISTICS.mode='topics';renderStatistics();statsSelect(payload.topics[0]);");assert(f.node('#stats-chart-title').textContent.includes('话题'));assert(f.node('#stats-selection').textContent.includes('100%'));tests++;}
 {const f=fixture();f.context.payload={...data,words:[]};f.run('STATISTICS.data=payload;renderStatistics()');assert.equal(f.node('#stats-bubbles').children[0].tag,'p');tests++;}
 // Late successful and late failed requests must not overwrite a newer filter.
 for(const rejectOld of [false,true]) {
  const f=fixture();f.context.responses=[];f.context.payload=data;f.run('api=()=>new Promise((resolve,reject)=>responses.push({resolve,reject}));');
  const a=f.run('loadStatistics()'),b=f.run('loadStatistics()');
  f.context.responses[1].resolve(data);await b;
  if(rejectOld)f.context.responses[0].reject(Error('old failure'));else f.context.responses[0].resolve({...data,summary:{...data.summary,messages:999}});
  await a;assert.equal(f.node('#stats-messages').textContent,'2');assert.equal(f.node('#btn-stats-refresh').disabled,false);tests++;
 }
 {const f=fixture();f.context.payload=data;f.run("STATISTICS.data=payload;renderStatistics();api=async()=>{throw Error('offline')}");await f.run('loadStatistics()');assert.equal(f.node('#stats-messages').textContent,'—');assert.equal(f.node('#stats-rank-total').textContent,'— 项');assert.equal(f.node('#btn-stats-refresh').disabled,false);tests++;}
 {const f=fixture();f.context.payload={...data,coverage:{omitted_bytes:999},summary:{...data.summary,unknown_platform_messages:1}};f.run('STATISTICS.data=payload;renderStatistics()');assert(f.node('#stats-status').textContent.includes('并非全部历史'));assert(f.node('#stats-status').textContent.includes('未标记平台'));tests++;}
 const html=fs.readFileSync(path.join(root,'src/web/index.html'),'utf8');assert(html.includes('data-page="statistics"'));assert(html.indexOf('/static/statistics.js')<html.indexOf('/static/app.js'));tests++;
 console.log(`STATISTICS_FRONTEND PASS tests=${tests} geometry_area=1 no_overlap=1 keyboard=1 xss_inert=1 stale_responses_ignored=1`);
}
main().catch(e=>{console.error(e);process.exitCode=1;});

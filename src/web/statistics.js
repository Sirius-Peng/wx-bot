/* Offline conversation analytics. Deterministic SVG packing; no CDN/chart service. */
'use strict';

function packBubbles(items, width, height) {
  width=Math.max(200,Number(width)||600);height=Math.max(200,Number(height)||400);
  const valid=items.filter(x=>Number.isFinite(x.count)&&x.count>0).slice(0,50);
  if(!valid.length)return [];
  const sum=valid.reduce((n,x)=>n+x.count,0),max=Math.max(...valid.map(x=>x.count));
  let scale=Math.min(Math.sqrt(width*height*.38/(Math.PI*sum)),Math.min(width,height)*.23/Math.sqrt(max));
  const gap=3,margin=12;
  for(let pass=0;pass<18;pass++,scale*=.9) {
    const circles=[];let fits=true;
    for(const item of valid) {
      const r=Math.sqrt(item.count)*scale;
      let placed=null;
      if(!circles.length)placed={...item,x:width/2,y:height/2,r};
      else for(let step=0;step<18000;step++) {
        const angle=step*.22,distance=Math.sqrt(step)*2.7;
        const x=width/2+Math.cos(angle)*distance,y=height/2+Math.sin(angle)*distance;
        if(x-r<margin||x+r>width-margin||y-r<margin||y+r>height-margin)continue;
        if(circles.every(c=>(x-c.x)**2+(y-c.y)**2>=(r+c.r+gap)**2)) {placed={...item,x,y,r};break;}
      }
      if(!placed){fits=false;break;}circles.push(placed);
    }
    if(fits)return circles;
  }
  return [];
}

function bubbleLabelLines(label, radius, fontSize) {
  const chars=Array.from(String(label)),capacity=Math.max(1,radius*1.5/fontSize);
  const units=ch=>/[\u0000-\u007f]/.test(ch)?(/[wm]/i.test(ch)?.8:.58):1;
  const lines=[];let index=0;
  while(index<chars.length&&lines.length<2) {
    let line='',used=0;
    while(index<chars.length&&(used+units(chars[index])<=capacity||!line)) {used+=units(chars[index]);line+=chars[index++];}
    lines.push(line);
  }
  if(index<chars.length) {
    const last=Array.from(lines[lines.length-1]);
    while(last.length&&last.reduce((sum,ch)=>sum+units(ch),0)+1>capacity)last.pop();
    lines[lines.length-1]=last.join('')+'…';
  }
  return lines;
}

// Geometry is independently testable in Node; UI bindings stay in the browser.
if(typeof module!=='undefined'&&module.exports)module.exports={packBubbles,bubbleLabelLines};

const STATISTICS={data:null,mode:'words',selected:'',generation:0,controller:null};
const STATS_COLORS=['#CDEAD8','#D5E6FA','#E4DEFA','#F4E2C7','#D4EEE9','#F1DDE5'];
function statsItems(){return STATISTICS.data?.[STATISTICS.mode]||[];}
function statsSelect(item) {
  STATISTICS.selected=item.label;
  const p=document.querySelector('#stats-selection');
  const count=Number(item.count).toLocaleString();
  p.textContent=STATISTICS.mode==='topics'
    ? `${item.label} · ${count} 条文本涉及，占有效文本 ${item.share}%${item.keywords?.length?' · 命中词：'+item.keywords.join('、'):''}`
    : `${item.label} · 出现 ${count} 次，涉及 ${Number(item.messages).toLocaleString()} 条消息。`;
  document.querySelectorAll('[data-stats-label]').forEach(el=>el.classList.toggle('selected',el.dataset.statsLabel===item.label));
}
function statsSvgNode(tag,attrs={},text) {
  const node=document.createElementNS('http://www.w3.org/2000/svg',tag);
  Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,String(v)));
  if(text!==undefined)node.textContent=text;
  return node;
}
function statsEmpty(box,text) {
  box.replaceChildren();const p=document.createElement('p');p.className='stats-empty';p.textContent=text;box.appendChild(p);
}
function renderStatsBubbles() {
  const box=document.querySelector('#stats-bubbles'),items=statsItems();
  if(!items.length){statsEmpty(box,'当前筛选下没有高频项。可降低最低次数或扩大时间范围。');return;}
  const width=Math.max(280,box.clientWidth||600),height=width<420?360:420;
  const circles=packBubbles(items,width,height);box.replaceChildren();
  if(!circles.length){statsEmpty(box,'气泡布局未完成，右侧排行仍可查看。');return;}
  const svg=statsSvgNode('svg',{viewBox:`0 0 ${width} ${height}`,width:'100%',height,role:'group','aria-label':STATISTICS.mode==='words'?'高频词气泡图，面积对应词次':'高频话题气泡图，面积对应涉及消息数'});
  circles.forEach((item,i)=>{
    const g=statsSvgNode('g',{role:'button',tabindex:0,'data-stats-label':item.label,'aria-label':`${item.label}，${item.count}${STATISTICS.mode==='words'?'次':'条文本'}，涉及${item.messages}条消息`});
    g.appendChild(statsSvgNode('circle',{cx:item.x,cy:item.y,r:item.r,fill:STATS_COLORS[i%STATS_COLORS.length]}));
    g.appendChild(statsSvgNode('title',{},`${item.label}：${item.count}；涉及 ${item.messages} 条消息`));
    if(item.r>=22) {
      const font=Math.max(11,Math.min(20,item.r*.26));
      const lines=bubbleLabelLines(item.label,item.r,font);
      const text=statsSvgNode('text',{'text-anchor':'middle',fill:'#243B31','font-size':font,'font-weight':600,'pointer-events':'none'});
      lines.forEach((line,j)=>text.appendChild(statsSvgNode('tspan',{x:item.x,y:item.y-font*.5+(j-(lines.length-1)/2)*font*1.12},line)));
      g.appendChild(text);
      g.appendChild(statsSvgNode('text',{x:item.x,y:item.y+font*(lines.length===2?1.45:1.2),'text-anchor':'middle',fill:'#395849','font-size':Math.max(10,font*.78),'pointer-events':'none'},Number(item.count).toLocaleString()));
    }
    g.addEventListener('click',()=>statsSelect(item));
    g.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();statsSelect(item);}});
    svg.appendChild(g);
  });
  box.appendChild(svg);
  const chosen=items.find(x=>x.label===STATISTICS.selected);if(chosen)statsSelect(chosen);
}
function renderStatistics() {
  const data=STATISTICS.data;if(!data)return;
  const summary=data.summary,items=statsItems();
  document.querySelector('#stats-messages').textContent=summary.messages.toLocaleString();
  document.querySelector('#stats-texts').textContent=`有效文本 ${summary.text_messages.toLocaleString()} 条`;
  document.querySelector('#stats-tokens').textContent=summary.token_count.toLocaleString();
  document.querySelector('#stats-unique').textContent=summary.unique_words.toLocaleString();
  document.querySelector('#stats-days-count').textContent=summary.active_days.toLocaleString();
  document.querySelector('#stats-timezone').textContent=data.filters.timezone;
  const isWord=STATISTICS.mode==='words';
  document.querySelector('#stats-chart-title').textContent=isWord?'高频词气泡图':'高频话题气泡图';
  document.querySelector('#stats-count-label').textContent=isWord?'词次':'涉及文本数';
  document.querySelector('#stats-caption').textContent=isWord?'气泡面积表示词语出现次数，排行同时显示涉及消息数。':'气泡面积表示涉及文本数；同一条文本可属于多个话题。';
  document.querySelector('#stats-rank-total').textContent=`${items.length} 项`;
  const ranking=document.querySelector('#stats-ranking');ranking.replaceChildren();
  const max=items[0]?.count||1;
  items.forEach((item,i)=>{
    const button=document.createElement('button');button.type='button';button.className='stats-rank-row';button.dataset.statsLabel=item.label;
    const number=document.createElement('span');number.className='stats-rank-number';number.textContent=String(i+1).padStart(2,'0');
    const main=document.createElement('span');main.className='stats-rank-main';
    const label=document.createElement('strong');label.textContent=item.label;
    const bar=document.createElement('span');bar.className='stats-rank-track';const fill=document.createElement('i');fill.style.width=`${item.count/max*100}%`;bar.appendChild(fill);
    main.append(label,bar);
    const total=document.createElement('span');total.className='stats-rank-value';total.textContent=`${item.count}${isWord?' 次':' 条'}`;
    const details=document.createElement('small');details.textContent=isWord?`${item.messages} 条消息`:`${item.share}%`;total.appendChild(details);
    button.append(number,main,total);button.addEventListener('click',()=>statsSelect(item));ranking.appendChild(button);
  });
  if(!items.length)statsEmpty(ranking,'暂无符合条件的统计项。');
  document.querySelector('#stats-selection').textContent=isWord?'选择一个气泡，查看词频和涉及消息数。':'选择一个话题，查看占比及命中词。';
  renderStatsBubbles();
  const coverage=data.coverage,notes=[];
  if(coverage.changed_during_scan)notes.push('分析期间日志有更新，下次刷新补齐');
  if(coverage.omitted_bytes)notes.push('日志较大，本次只扫描最近 32 MiB，并非全部历史');
  if(coverage.skipped_lines||coverage.oversized_lines)notes.push(`跳过 ${coverage.skipped_lines+coverage.oversized_lines} 条损坏、未完成或超长记录`);
  if(coverage.truncated_texts)notes.push(`${coverage.truncated_texts} 条超长文本仅取前 8000 字符`);
  if(coverage.vocabulary_limited)notes.push('词汇达到 30000 项上限，部分新词未计入');
  if(summary.unknown_platform_messages)notes.push('包含未标记平台的旧记录；它们仅在“微信与 QQ”中显示');
  document.querySelector('#stats-status').textContent=notes.length?notes.join('；'):`已更新 · ${data.generated_at} · 筛选统计以本地记录为准`;
}
async function loadStatistics() {
  const generation=++STATISTICS.generation;STATISTICS.controller?.abort();const controller=new AbortController();STATISTICS.controller=controller;
  const button=document.querySelector('#btn-stats-refresh');button.disabled=true;
  document.querySelector('#stats-status').textContent='正在本地分析聊天记录…';
  const q=new URLSearchParams({days:document.querySelector('#stats-days').value,platform:document.querySelector('#stats-platform').value,direction:document.querySelector('#stats-direction').value,chat_type:document.querySelector('#stats-chat-type').value,limit:document.querySelector('#stats-limit').value,min_count:document.querySelector('#stats-min-count').value});
  try {
    const data=await api('/api/statistics?'+q,{signal:controller.signal});
    if(generation!==STATISTICS.generation)return;
    STATISTICS.data=data;renderStatistics();
  } catch(error) {
    if(generation!==STATISTICS.generation)return;
    STATISTICS.data=null;
    ['#stats-messages','#stats-tokens','#stats-unique','#stats-days-count'].forEach(id=>document.querySelector(id).textContent='—');
    document.querySelector('#stats-texts').textContent='有效文本 — 条';
    document.querySelector('#stats-rank-total').textContent='— 项';
    statsEmpty(document.querySelector('#stats-bubbles'),'统计读取失败，请刷新重试。');statsEmpty(document.querySelector('#stats-ranking'),'暂无统计结果。');
    document.querySelector('#stats-selection').textContent='';
    document.querySelector('#stats-status').textContent='统计读取失败：'+error.message;
  } finally {if(generation===STATISTICS.generation)button.disabled=false;}
}
function bindStatistics() {
  document.querySelector('#btn-stats-refresh').addEventListener('click',loadStatistics);
  ['#stats-days','#stats-platform','#stats-direction','#stats-chat-type','#stats-limit','#stats-min-count'].forEach(id=>document.querySelector(id).addEventListener('change',()=>{STATISTICS.selected='';loadStatistics();}));
  document.querySelectorAll('#stats-mode button').forEach(button=>button.addEventListener('click',()=>{
    STATISTICS.mode=button.dataset.mode;STATISTICS.selected='';
    document.querySelectorAll('#stats-mode button').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-pressed',String(b===button));});
    renderStatistics();
  }));
  if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{if(STATISTICS.data&&document.querySelector('#page-statistics').classList.contains('active'))renderStatsBubbles();}).observe(document.querySelector('#stats-bubbles'));
}

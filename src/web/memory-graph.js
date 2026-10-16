/* Local, read-only graph. Layout and camera live only in RAM; viewing never recalls or strengthens memory. */
'use strict';
const MEMORY_GRAPH={data:null,selected:'',hovered:'',focused:'',generation:0,controller:null,zoom:1,pan:{x:0,y:0},positions:[],timer:null,
  mode:'global',localRoot:'',depth:1,types:new Set(['person','memory','conversation','topic']),density:1,nodeScale:1,
  layoutCache:new Map(),layoutData:null,scene:null,bound:false,resizeObserver:null,fullscreenFallback:false};
const GRAPH_TYPES={person:'对象',memory:'记忆',conversation:'对话 / 来源摘录',topic:'关键词'};

function graphNodeRadius(degree,scale=1) {
  return (3.5+Math.min(6.5,Math.sqrt(Math.max(0,Number(degree)||0))*1.65))*Math.max(.7,Math.min(1.6,Number(scale)||1));
}
function graphLayout(nodes,edges,width,height,options={}) {
  width=Number.isFinite(Number(width))?Math.max(160,Number(width)||600):600;
  height=Number.isFinite(Number(height))?Math.max(160,Number(height)||440):440;
  const source=nodes.slice(0,300),ids=new Set(source.map(n=>n.id)),degree=new Map(source.map(n=>[n.id,new Set()]));
  const valid=edges.filter(e=>ids.has(e.source)&&ids.has(e.target)&&e.source!==e.target);
  valid.forEach(e=>{degree.get(e.source).add(e.target);degree.get(e.target).add(e.source);});
  const list=source.map((n,i)=>({...n,degree:degree.get(n.id).size,r:graphNodeRadius(degree.get(n.id).size,options.nodeScale),
    x:width/2+Math.cos(i*2.399963)*Math.sqrt((i+1)/Math.max(1,source.length))*width*.36,
    y:height/2+Math.sin(i*2.399963)*Math.sqrt((i+1)/Math.max(1,source.length))*height*.36}));
  const byId=new Map(list.map(n=>[n.id,n])),index=new Map(list.map((n,i)=>[n.id,i]));
  for(let step=0;step<75;step++) {
    const forces=list.map(()=>({x:0,y:0}));
    for(let i=0;i<list.length;i++)for(let j=i+1;j<list.length;j++){
      const a=list[i],b=list[j];let dx=a.x-b.x,dy=a.y-b.y;
      if(!dx&&!dy){dx=.1*(i+1);dy=.1*(j+1);}
      const distance=Math.max(1,Math.hypot(dx,dy)),desired=a.r+b.r+22;
      const push=Math.min(5,distance<desired?(desired-distance)*.3:650/(distance*distance));
      const fx=dx/distance*push,fy=dy/distance*push;
      forces[i].x+=fx;forces[i].y+=fy;forces[j].x-=fx;forces[j].y-=fy;
    }
    valid.forEach(e=>{
      const a=byId.get(e.source),b=byId.get(e.target),dx=b.x-a.x,dy=b.y-a.y,d=Math.max(1,Math.hypot(dx,dy));
      const pull=(d-(e.kind==='keyword'?66:88))*.018,fx=dx/d*pull,fy=dy/d*pull;
      forces[index.get(a.id)].x+=fx;forces[index.get(a.id)].y+=fy;
      forces[index.get(b.id)].x-=fx;forces[index.get(b.id)].y-=fy;
    });
    list.forEach((n,i)=>{
      const pad=n.r+8;
      n.x=Math.max(pad,Math.min(width-pad,n.x+forces[i].x+(width/2-n.x)*.0015));
      n.y=Math.max(pad,Math.min(height-pad,n.y+forces[i].y+(height/2-n.y)*.0015));
    });
  }
  if(list.length>1){
    const xs=list.map(n=>n.x),ys=list.map(n=>n.y),loX=Math.min(...xs),hiX=Math.max(...xs),loY=Math.min(...ys),hiY=Math.max(...ys);
    const padX=Math.min(55,width*.2),padY=Math.min(40,height*.2);
    const scale=Math.min(1.8,(width-padX*2)/Math.max(1,hiX-loX),(height-padY*2)/Math.max(1,hiY-loY));
    list.forEach(n=>{n.x=Math.max(n.r,Math.min(width-n.r,width/2+(n.x-(loX+hiX)/2)*scale));n.y=Math.max(n.r,Math.min(height-n.r,height/2+(n.y-(loY+hiY)/2)*scale));});
  }
  return list;
}
function graphNeighborhood(data,id) {
  const result=new Set([id]);
  (data.edges||[]).forEach(e=>{if(e.source===id)result.add(e.target);if(e.target===id)result.add(e.source);});return result;
}
function graphLocalData(data,root,depth=1) {
  const nodes=data?.nodes||[],edges=data?.edges||[],ids=new Set(nodes.map(n=>n.id));
  if(!ids.has(root))return {...data,nodes:[],edges:[]};
  const adjacency=new Map(nodes.map(n=>[n.id,new Set()]));
  edges.forEach(e=>{if(ids.has(e.source)&&ids.has(e.target)){adjacency.get(e.source).add(e.target);adjacency.get(e.target).add(e.source);}});
  const included=new Set([root]);let frontier=[root];
  for(let hop=0;hop<Math.max(1,Math.min(3,Math.floor(Number(depth)||1)));hop++){
    const next=[];frontier.forEach(id=>adjacency.get(id).forEach(other=>{if(!included.has(other)){included.add(other);next.push(other);}}));frontier=next;
  }
  return {...data,nodes:nodes.filter(n=>included.has(n.id)),edges:edges.filter(e=>included.has(e.source)&&included.has(e.target))};
}
function graphFilterTypes(data,enabledTypes) {
  const types=enabledTypes instanceof Set?enabledTypes:new Set(enabledTypes||[]),nodes=(data?.nodes||[]).filter(n=>types.has(n.type)),ids=new Set(nodes.map(n=>n.id));
  return {...data,nodes,edges:(data?.edges||[]).filter(e=>ids.has(e.source)&&ids.has(e.target))};
}
function graphZoomAt(oldZoom,newZoom,pan,anchor) {
  const old=Math.max(.001,Number(oldZoom)||1),zoom=Math.min(3,Math.max(.5,Number(newZoom)||1));
  return {zoom,pan:{x:anchor.x-(anchor.x-pan.x)*zoom/old,y:anchor.y-(anchor.y-pan.y)*zoom/old}};
}
/* Screen-space labels: fixed readable size, zoom-dependent budget and bounded spatial-grid collision checks. */
function graphLabelPlan(positions,options={}) {
  const {zoom=1,pan={x:0,y:0},width=600,height=440,density=1,selected='',focus=''}=options;
  const result=new Map(),cells=new Map(),cellSize=64;
  const ranked=positions.slice().sort((a,b)=>Number(b.id===focus)-Number(a.id===focus)||Number(b.id===selected)-Number(a.id===selected)||(b.degree||0)-(a.degree||0)||String(a.id).localeCompare(String(b.id)));
  const budget=density===0?0:density===2?Math.min(300,Math.round(100*zoom)):Math.min(160,Math.round(24*zoom*zoom));
  let ordinary=0;
  for(const n of ranked){
    const protectedLabel=n.id===selected||n.id===focus;
    if(!protectedLabel&&(density===0||ordinary>=budget))continue;
    const x=n.x*zoom+pan.x,y=n.y*zoom+pan.y;
    if(x<-20||x>width+20||y<-20||y>height+20)continue;
    const full=String(n.label||''),chars=Array.from(full),max=zoom>=1.8?25:zoom>=1?17:12,text=chars.length>max?chars.slice(0,max-1).join('')+'…':full;
    const measure=Array.from(text).reduce((sum,c)=>sum+(/[\u0000-\u00ff]/.test(c)?6.5:12),0),w=Math.min(width-8,measure+8),h=19;
    const left=Math.max(4,Math.min(width-w-4,x-w/2)),top=y+(n.r||5)*zoom+5;
    const box={x:left,y:top+h>height-3?y-(n.r||5)*zoom-h-5:top,w,h};
    const keys=[];for(let cx=Math.floor((box.x-4)/cellSize);cx<=Math.floor((box.x+box.w+4)/cellSize);cx++)for(let cy=Math.floor((box.y-3)/cellSize);cy<=Math.floor((box.y+box.h+3)/cellSize);cy++)keys.push(`${cx}:${cy}`);
    let collides=false;
    for(const key of keys){for(const other of cells.get(key)||[]){if(box.x<other.x+other.w+4&&box.x+box.w+4>other.x&&box.y<other.y+other.h+3&&box.y+box.h+3>other.y){collides=true;break;}}if(collides)break;}
    if(collides&&!protectedLabel)continue;
    result.set(n.id,{text,x:(box.x+w/2-x)/zoom,y:(box.y+14-y)/zoom,fontSize:12/zoom});
    keys.forEach(key=>{if(!cells.has(key))cells.set(key,[]);cells.get(key).push(box);});if(!protectedLabel)ordinary++;
  }
  return result;
}
function graphElement(tag,text,className) {
  const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;
}
function graphSvg(tag,attrs={}) {
  const el=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));return el;
}
function graphSetClass(el,name,value) {if(el?.classList?.toggle)el.classList.toggle(name,!!value);}
function graphDate(value) {
  if(!value)return '时间未记录';const date=new Date(value*1000);return Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{hour12:false}):'时间未记录';
}
function graphSelect(id) {
  const node=MEMORY_GRAPH.data?.nodes.find(n=>n.id===id);MEMORY_GRAPH.selected=node?id:'';
  if(node){const settings=document.querySelector('#graph-settings');if(settings)settings.open=false;}
  // Selecting and following a relation never reruns forces, moves the camera or changes the local root.
  graphHighlight();renderGraphDetails();
}
function renderGraphDetails() {
  const box=document.querySelector('#graph-details');if(!box)return;box.replaceChildren();
  const data=MEMORY_GRAPH.data,node=data?.nodes.find(n=>n.id===MEMORY_GRAPH.selected),shell=document.querySelector('#graph-details-shell');
  if(shell)shell.hidden=!node||!!document.querySelector('#graph-settings')?.open;
  if(!node){box.appendChild(graphElement('p','点选节点，追溯原话与关联。','hint'));return;}
  box.append(graphElement('span',GRAPH_TYPES[node.type]||'节点','graph-kind'),graphElement('h3',node.label),graphElement('p',node.body||'未记录正文','graph-body'));
  if(node.type==='memory') {
    box.appendChild(graphElement('p',`${node.phaseLabel} · ${node.status==='active'?'当前':'历史 / 已更正'} · 记忆强度 ${Math.round((node.strength||0)*100)}%`,'hint'));
    box.appendChild(graphElement('p',`${node.source_note||'来源未记录'} · 用户依据 ${node.evidence_count||0} 次`,'hint'));box.appendChild(graphElement('p',graphDate(node.ts),'hint'));
  } else if(node.type==='conversation') {
    box.appendChild(graphElement('p',`${node.platform==='ilink'?'微信':node.platform==='onebot'?'QQ':'旧版来源'} · ${graphDate(node.ts)}`,'hint'));
    box.appendChild(graphElement('p',node.origin==='evidence_excerpt'?'这是保留的逐字依据摘录，原始对话已压缩或当前不可用。':'来自近期上下文；一次交流不等于已经形成长期记忆。','hint'));
    if(node.assistant)box.append(graphElement('h4','当时的回复'),graphElement('p',node.assistant,'graph-body'));
  } else if(node.type==='person') {
    (node.accounts||[]).forEach(a=>box.appendChild(graphElement('p',`${a.platform==='ilink'?'微信':a.platform==='onebot'?'QQ':'通用'} · ${a.owner_id||'共享手动记录'}`,'hint')));
  } else if(node.source_note)box.appendChild(graphElement('p',node.source_note,'hint'));
  const linked=(data.edges||[]).filter(e=>e.source===node.id||e.target===node.id),byId=new Map(data.nodes.map(n=>[n.id,n]));
  box.appendChild(graphElement('p',`节点大小表示直接关联数量（${new Set(linked.map(e=>e.source===node.id?e.target:e.source)).size}），不代表记忆强度。`,'hint'));
  box.appendChild(graphElement('h4','直接关联'));
  if(!linked.length)box.appendChild(graphElement('p','当前读取范围内没有直接关联。','hint'));
  linked.forEach(edge=>{
    const other=byId.get(edge.source===node.id?edge.target:edge.source);if(!other)return;
    const button=graphElement('button',`${edge.label}${edge.derived?'（字面关联）':''} → ${other.label}`,'graph-relation');button.type='button';
    button.addEventListener('click',()=>graphSelect(other.id));box.appendChild(button);
  });
}
function graphViewData() {
  let data=MEMORY_GRAPH.data||{nodes:[],edges:[]};
  if(MEMORY_GRAPH.mode==='local')data=graphLocalData(data,MEMORY_GRAPH.localRoot,MEMORY_GRAPH.depth);
  return graphFilterTypes(data,MEMORY_GRAPH.types);
}
function graphDimensions() {
  const host=document.querySelector('#graph-canvas');return {width:Math.max(160,host?.clientWidth||600),height:Math.max(160,host?.clientHeight||440)};
}
function graphTransform() {
  const group=MEMORY_GRAPH.scene?.group||document.querySelector('#memory-graph-transform');
  if(group)group.setAttribute('transform',`translate(${MEMORY_GRAPH.pan.x} ${MEMORY_GRAPH.pan.y}) scale(${MEMORY_GRAPH.zoom})`);
  const label=document.querySelector('#graph-zoom-label');if(label)label.textContent=`${Math.round(MEMORY_GRAPH.zoom*100)}%`;graphLabels();
}
function graphLabels() {
  const scene=MEMORY_GRAPH.scene;if(!scene)return;
  const plan=graphLabelPlan(scene.positions,{zoom:MEMORY_GRAPH.zoom,pan:MEMORY_GRAPH.pan,width:scene.width,height:scene.height,density:MEMORY_GRAPH.density,selected:MEMORY_GRAPH.selected,focus:MEMORY_GRAPH.hovered||MEMORY_GRAPH.focused});
  scene.nodes.forEach((entry,id)=>{
    const label=plan.get(id);entry.label.setAttribute('display',label?'':'none');
    if(label){entry.label.textContent=label.text;entry.label.setAttribute('x',label.x);entry.label.setAttribute('y',label.y);entry.label.setAttribute('style',`font-size:${label.fontSize}px`);}
  });
}
function graphHighlight() {
  const scene=MEMORY_GRAPH.scene;if(!scene)return;
  const candidate=MEMORY_GRAPH.hovered||MEMORY_GRAPH.focused||MEMORY_GRAPH.selected,focus=scene.byId.has(candidate)?candidate:'',near=focus?scene.adjacency.get(focus):null;
  scene.nodes.forEach((entry,id)=>{
    const selected=id===MEMORY_GRAPH.selected,muted=!!near&&!near.has(id);
    entry.group.setAttribute('class',`graph-node graph-${entry.node.type}${selected?' selected':''}${muted?' muted':''}${id===MEMORY_GRAPH.hovered?' hovered':''}${id===MEMORY_GRAPH.focused?' focused':''}`);
    entry.group.setAttribute('aria-pressed',String(selected));
  });
  scene.edges.forEach(entry=>entry.line.setAttribute('class',`graph-edge${entry.edge.derived?' derived':''}${focus&&(entry.edge.source===focus||entry.edge.target===focus)?' highlighted':focus?' muted':''}`));
  graphLabels();
}
function graphZoom(factor,anchor) {
  const {width,height}=MEMORY_GRAPH.scene||graphDimensions();
  const next=graphZoomAt(MEMORY_GRAPH.zoom,MEMORY_GRAPH.zoom*factor,MEMORY_GRAPH.pan,anchor||{x:width/2,y:height/2});
  MEMORY_GRAPH.zoom=next.zoom;MEMORY_GRAPH.pan=next.pan;graphTransform();
}
function graphFit() {
  const scene=MEMORY_GRAPH.scene;
  if(!scene?.positions.length){MEMORY_GRAPH.zoom=1;MEMORY_GRAPH.pan={x:0,y:0};graphTransform();return;}
  const list=scene.positions,minX=Math.min(...list.map(n=>n.x-n.r)),maxX=Math.max(...list.map(n=>n.x+n.r)),minY=Math.min(...list.map(n=>n.y-n.r)),maxY=Math.max(...list.map(n=>n.y+n.r));
  const top=Math.min(80,scene.height*.18),bottom=Math.min(125,scene.height*.24),usableHeight=Math.max(60,scene.height-top-bottom);
  const zoom=Math.min(2,Math.max(.5,Math.min((scene.width-100)/Math.max(1,maxX-minX),usableHeight/Math.max(1,maxY-minY))));
  MEMORY_GRAPH.zoom=zoom;MEMORY_GRAPH.pan={x:scene.width/2-(minX+maxX)/2*zoom,y:top+usableHeight/2-(minY+maxY)/2*zoom};graphTransform();
}
function graphPoint(event,scene) {
  const rect=scene.svg.getBoundingClientRect?.()||{left:0,top:0,width:scene.width,height:scene.height};
  return {x:(event.clientX-rect.left)*scene.width/(rect.width||scene.width),y:(event.clientY-rect.top)*scene.height/(rect.height||scene.height)};
}
function graphUpdateNode(scene,id) {
  const entry=scene.nodes.get(id);if(!entry)return;const n=entry.node;entry.group.setAttribute('transform',`translate(${n.x} ${n.y})`);
  (scene.incident.get(id)||[]).forEach(edge=>{const a=scene.byId.get(edge.edge.source),b=scene.byId.get(edge.edge.target);edge.line.setAttribute('x1',a.x);edge.line.setAttribute('y1',a.y);edge.line.setAttribute('x2',b.x);edge.line.setAttribute('y2',b.y);});
}
function graphBindPointers(scene) {
  const {svg}=scene,pointers=new Map();let drag=null,pinch=null;
  const release=id=>{try{if(svg.hasPointerCapture?.(id))svg.releasePointerCapture(id);}catch(_){/* capture may already have ended */}};
  const clear=()=>{pointers.forEach((_,id)=>release(id));pointers.clear();drag=null;pinch=null;};scene.cleanup=clear;
  svg.addEventListener('pointerdown',event=>{
    if(event.button!==undefined&&event.button!==0)return;
    const point=graphPoint(event,scene);pointers.set(event.pointerId,point);try{svg.setPointerCapture?.(event.pointerId);}catch(_){}
    if(pointers.size===2){const [a,b]=[...pointers.values()];pinch={distance:Math.max(1,Math.hypot(a.x-b.x,a.y-b.y)),anchor:{x:(a.x+b.x)/2,y:(a.y+b.y)/2},zoom:MEMORY_GRAPH.zoom,pan:{...MEMORY_GRAPH.pan}};drag=null;scene.suppressClickUntil=Date.now()+400;event.preventDefault();return;}
    const group=event.target.closest?.('[data-node-id]'),id=group?.getAttribute?.('data-node-id'),node=scene.byId.get(id);
    drag={id:node?id:'',pointer:event.pointerId,start:point,last:point,pan:{...MEMORY_GRAPH.pan},nodeX:node?.x,nodeY:node?.y,moved:false};event.preventDefault();
  });
  svg.addEventListener('pointermove',event=>{
    if(!pointers.has(event.pointerId))return;const point=graphPoint(event,scene);pointers.set(event.pointerId,point);
    if(pinch&&pointers.size>=2){const [a,b]=[...pointers.values()],mid={x:(a.x+b.x)/2,y:(a.y+b.y)/2},distance=Math.max(1,Math.hypot(a.x-b.x,a.y-b.y));
      const next=graphZoomAt(pinch.zoom,pinch.zoom*distance/pinch.distance,pinch.pan,pinch.anchor);next.pan.x+=mid.x-pinch.anchor.x;next.pan.y+=mid.y-pinch.anchor.y;
      MEMORY_GRAPH.zoom=next.zoom;MEMORY_GRAPH.pan=next.pan;graphTransform();event.preventDefault();return;}
    if(!drag||drag.pointer!==event.pointerId)return;
    const dx=point.x-drag.start.x,dy=point.y-drag.start.y;if(Math.hypot(dx,dy)>4)drag.moved=true;
    if(drag.moved){if(drag.id){const n=scene.byId.get(drag.id);n.x=drag.nodeX+dx/MEMORY_GRAPH.zoom;n.y=drag.nodeY+dy/MEMORY_GRAPH.zoom;graphUpdateNode(scene,drag.id);graphLabels();}
      else{MEMORY_GRAPH.pan={x:drag.pan.x+dx,y:drag.pan.y+dy};graphTransform();}}event.preventDefault();
  });
  svg.addEventListener('pointerup',event=>{
    const wasPinch=!!pinch;if(drag?.pointer===event.pointerId){
      if(drag.moved)scene.suppressClickUntil=Date.now()+400;
      else {graphSelect(drag.id||'');scene.suppressClickUntil=Date.now()+400;}
    }
    release(event.pointerId);pointers.delete(event.pointerId);drag=null;pinch=null;
    if(wasPinch){scene.suppressClickUntil=Date.now()+400;clear();}graphLabels();
  });
  svg.addEventListener('pointercancel',clear);svg.addEventListener('lostpointercapture',event=>{pointers.delete(event.pointerId);if(drag?.pointer===event.pointerId)drag=null;pinch=null;});
  svg.addEventListener('wheel',event=>{event.preventDefault();graphZoom(Math.exp(-Math.max(-500,Math.min(500,event.deltaY))*.0015),graphPoint(event,scene));},{passive:false});
  svg.addEventListener('keydown',event=>{
    if(event.key==='Escape'){event.preventDefault();graphSelect('');if(MEMORY_GRAPH.fullscreenFallback)graphExitFullscreen();return;}
    if(['+','=','-','_','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key)){
      event.preventDefault();if(event.key==='+'||event.key==='=')graphZoom(1.2);else if(event.key==='-'||event.key==='_')graphZoom(1/1.2);
      else{MEMORY_GRAPH.pan.x+=event.key==='ArrowLeft'?35:event.key==='ArrowRight'?-35:0;MEMORY_GRAPH.pan.y+=event.key==='ArrowUp'?35:event.key==='ArrowDown'?-35:0;graphTransform();}
    }
  });
}
function renderMemoryGraph() {
  const host=document.querySelector('#graph-canvas');if(!host)return;MEMORY_GRAPH.scene?.cleanup?.();MEMORY_GRAPH.scene=null;host.replaceChildren();
  const data=MEMORY_GRAPH.data,view=graphViewData();
  if(!view.nodes.length){host.appendChild(graphElement('p',data?.nodes?.length?'当前显示条件下没有节点。请打开分组或切回全局图谱。':'还没有可展示的关联。先积累对话或手动添加记忆，也可以调整筛选。','graph-empty'));return;}
  if(MEMORY_GRAPH.layoutData!==data){MEMORY_GRAPH.layoutCache.clear();MEMORY_GRAPH.layoutData=data;}
  const {width,height}=graphDimensions(),key=`${width}:${height}`;
  let allPositions=MEMORY_GRAPH.layoutCache.get(key);
  if(!allPositions){const inset=height>320?160:0;allPositions=graphLayout(data.nodes,data.edges,width,height-inset);if(inset)allPositions.forEach(n=>n.y+=60);MEMORY_GRAPH.layoutCache.set(key,allPositions);if(MEMORY_GRAPH.layoutCache.size>4)MEMORY_GRAPH.layoutCache.delete(MEMORY_GRAPH.layoutCache.keys().next().value);}
  const ids=new Set(view.nodes.map(n=>n.id)),positions=allPositions.filter(n=>ids.has(n.id));positions.forEach(n=>n.r=graphNodeRadius(n.degree,MEMORY_GRAPH.nodeScale));MEMORY_GRAPH.positions=positions;
  const svg=graphSvg('svg',{viewBox:`0 0 ${width} ${height}`,role:'group',tabindex:'0','aria-label':'记忆关联网络：拖动节点整理，拖动画布平移，滚轮或加减键缩放，方向键平移，Escape 清除选择；下方有完整节点列表'});
  const title=graphSvg('title');title.textContent='对象、记忆、用户原话与关键词的关联；节点大小表示关联数量而非记忆强度';svg.appendChild(title);
  const group=graphSvg('g',{id:'memory-graph-transform'});svg.appendChild(group);
  const scene={svg,group,width,height,positions,byId:new Map(positions.map(n=>[n.id,n])),nodes:new Map(),edges:[],adjacency:new Map(positions.map(n=>[n.id,new Set([n.id])])),incident:new Map(),suppressClickUntil:0};MEMORY_GRAPH.scene=scene;
  view.edges.forEach(e=>{
    const a=scene.byId.get(e.source),b=scene.byId.get(e.target);if(!a||!b)return;
    const line=graphSvg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:`graph-edge${e.derived?' derived':''}`,'vector-effect':'non-scaling-stroke'}),entry={line,edge:e};group.appendChild(line);scene.edges.push(entry);
    scene.adjacency.get(a.id).add(b.id);scene.adjacency.get(b.id).add(a.id);[a.id,b.id].forEach(id=>{if(!scene.incident.has(id))scene.incident.set(id,[]);scene.incident.get(id).push(entry);});
  });
  positions.forEach(n=>{
    const g=graphSvg('g',{transform:`translate(${n.x} ${n.y})`,class:`graph-node graph-${n.type}`,role:'button',tabindex:'0','aria-label':`${GRAPH_TYPES[n.type]}：${n.label}，${n.degree} 个关联`,'aria-pressed':'false','data-node-id':n.id});
    g.appendChild(graphSvg('circle',{r:Math.max(14,n.r+5),class:'graph-hit'}));g.appendChild(graphSvg('circle',{r:n.r,class:'graph-shape','vector-effect':'non-scaling-stroke'}));
    const tooltip=graphSvg('title');tooltip.textContent=n.label;g.appendChild(tooltip);
    const label=graphSvg('text',{'text-anchor':'middle','aria-hidden':'true'});g.appendChild(label);scene.nodes.set(n.id,{group:g,label,node:n});
    g.addEventListener('click',event=>{event?.stopPropagation?.();if(Date.now()<scene.suppressClickUntil)return;graphSelect(n.id);});
    g.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation?.();graphSelect(n.id);}});
    g.addEventListener('pointerenter',()=>{MEMORY_GRAPH.hovered=n.id;graphHighlight();});g.addEventListener('pointerleave',()=>{if(MEMORY_GRAPH.hovered===n.id){MEMORY_GRAPH.hovered='';graphHighlight();}});
    g.addEventListener('focus',()=>{MEMORY_GRAPH.focused=n.id;graphHighlight();});g.addEventListener('blur',()=>{if(MEMORY_GRAPH.focused===n.id){MEMORY_GRAPH.focused='';graphHighlight();}});group.appendChild(g);
  });
  graphBindPointers(scene);host.appendChild(svg);graphTransform();graphHighlight();
}
function graphRenderList() {
  const list=document.querySelector('#graph-node-list');if(!list)return;list.replaceChildren();
  graphViewData().nodes.forEach(n=>{const button=graphElement('button',`${GRAPH_TYPES[n.type]} · ${n.label}`,'graph-list-node');button.type='button';button.addEventListener('click',()=>graphSelect(n.id));list.appendChild(button);});
}
function graphUpdateSummary() {
  const data=MEMORY_GRAPH.data;if(!data)return;
  const view=graphViewData(),count={person:0,memory:0,conversation:0,topic:0};view.nodes.forEach(n=>{if(n.type in count)count[n.type]++;});
  document.querySelector('#graph-summary').textContent=`${count.person} 个对象 · ${count.memory} 条记忆 · ${count.conversation} 段对话 / 摘录 · ${count.topic} 个关键词`;
  const coverage=data.coverage||{},notes=[];
  if(MEMORY_GRAPH.mode==='local'){
    const root=data.nodes.find(n=>n.id===MEMORY_GRAPH.localRoot),label=Array.from(String(root?.label||'所选节点')).slice(0,16).join('');
    notes.push(`局部 · ${MEMORY_GRAPH.depth} 层 · 中心：${label}。`);
  }
  if(view.nodes.length!==data.nodes.length)notes.push(`当前显示 ${view.nodes.length} / ${data.nodes.length} 个已读取节点。`);
  if(coverage.scope)notes.push(coverage.scope);
  if(coverage.truncated)notes.push(`已达到 ${coverage.limit} 节点上限，请选择对象或搜索以展开局部`);
  if(coverage.invalid_items||coverage.omitted_context_turns)notes.push('部分损坏、超限或缺少可验证来源的记录未展示');
  document.querySelector('#graph-status').textContent=notes.join(' ');
}
function graphPresentationChanged() {
  document.querySelector('#graph-mode-global')?.setAttribute('aria-pressed',String(MEMORY_GRAPH.mode==='global'));
  document.querySelector('#graph-mode-local')?.setAttribute('aria-pressed',String(MEMORY_GRAPH.mode==='local'));
  const depth=document.querySelector('#graph-depth');if(depth)depth.disabled=MEMORY_GRAPH.mode!=='local';
  renderMemoryGraph();graphRenderList();renderGraphDetails();graphUpdateSummary();
}
function renderGraphResult() {
  const data=MEMORY_GRAPH.data,selector=document.querySelector('#graph-person'),previous=selector.value;
  selector.replaceChildren();const all=graphElement('option','所有对象');all.value='';selector.appendChild(all);
  (data.people||[]).forEach(p=>{const option=graphElement('option',p.label);option.value=p.id;selector.appendChild(option);});selector.value=(data.people||[]).some(p=>p.id===previous)?previous:'';
  const count=data.stats;document.querySelector('#graph-summary').textContent=`${count.person} 个对象 · ${count.memory} 条记忆 · ${count.conversation} 段对话 / 摘录 · ${count.topic} 个关键词`;
  const coverage=data.coverage,notes=[coverage.scope];
  if(coverage.truncated)notes.push(`已达到 ${coverage.limit} 节点上限，请选择对象或搜索以展开局部`);
  if(coverage.invalid_items||coverage.omitted_context_turns)notes.push('部分损坏、超限或缺少可验证来源的记录未展示');
  document.querySelector('#graph-status').textContent=notes.join(' ');document.querySelector('#graph-notice').textContent=data.notice;
  if(!data.nodes.some(n=>n.id===MEMORY_GRAPH.selected))MEMORY_GRAPH.selected='';
  if(!data.nodes.some(n=>n.id===MEMORY_GRAPH.localRoot)){MEMORY_GRAPH.localRoot='';MEMORY_GRAPH.mode='global';}graphPresentationChanged();
}
function graphResetData() {
  MEMORY_GRAPH.data=null;MEMORY_GRAPH.selected='';MEMORY_GRAPH.hovered='';MEMORY_GRAPH.focused='';MEMORY_GRAPH.localRoot='';MEMORY_GRAPH.mode='global';MEMORY_GRAPH.positions=[];
  MEMORY_GRAPH.scene?.cleanup?.();MEMORY_GRAPH.scene=null;MEMORY_GRAPH.layoutCache.clear();MEMORY_GRAPH.layoutData=null;
}
async function loadMemoryGraph() {
  const generation=++MEMORY_GRAPH.generation;MEMORY_GRAPH.controller?.abort();const controller=new AbortController();MEMORY_GRAPH.controller=controller;
  const button=document.querySelector('#btn-graph-refresh');button.disabled=true;graphResetData();
  document.querySelector('#graph-canvas').replaceChildren(graphElement('p','正在读取本地关联…','graph-empty'));document.querySelector('#graph-node-list').replaceChildren();renderGraphDetails();
  document.querySelector('#graph-summary').textContent='正在读取拓扑';document.querySelector('#graph-status').textContent='正在读取本地记忆与对话关联…';
  const q=new URLSearchParams({platform:document.querySelector('#graph-platform').value,person:document.querySelector('#graph-person').value,phase:document.querySelector('#graph-phase').value,keyword:document.querySelector('#graph-search').value.trim(),include_history:document.querySelector('#graph-history').checked,limit:document.querySelector('#graph-limit').value});
  try {const data=await api('/api/memory/graph?'+q,{signal:controller.signal});if(generation!==MEMORY_GRAPH.generation)return;MEMORY_GRAPH.data=data;renderGraphResult();}
  catch(error){if(generation!==MEMORY_GRAPH.generation)return;graphResetData();document.querySelector('#graph-canvas').replaceChildren(graphElement('p','拓扑读取失败，刷新后重试。','graph-empty'));document.querySelector('#graph-summary').textContent='暂无可用拓扑';document.querySelector('#graph-node-list').replaceChildren();renderGraphDetails();document.querySelector('#graph-status').textContent='读取失败：'+error.message;}
  finally{if(generation===MEMORY_GRAPH.generation)button.disabled=false;}
}
function graphSyncFullscreen() {
  const workspace=document.querySelector('#graph-workspace'),active=document.fullscreenElement===workspace||MEMORY_GRAPH.fullscreenFallback;
  graphSetClass(workspace,'is-fullscreen',active);const button=document.querySelector('#graph-fullscreen');button?.setAttribute('aria-pressed',String(active));button?.setAttribute('aria-label',active?'退出全屏':'全屏查看图谱');button?.setAttribute('title',active?'退出全屏':'全屏查看图谱');
}
function graphExitFullscreen() {
  MEMORY_GRAPH.fullscreenFallback=false;const workspace=document.querySelector('#graph-workspace');
  if(workspace&&document.fullscreenElement===workspace)Promise.resolve(document.exitFullscreen?.()).catch(()=>{});graphSyncFullscreen();
}
async function graphToggleFullscreen() {
  const workspace=document.querySelector('#graph-workspace');if(!workspace)return;
  if(document.fullscreenElement===workspace||MEMORY_GRAPH.fullscreenFallback){graphExitFullscreen();return;}
  if(workspace.requestFullscreen){try{await workspace.requestFullscreen();}catch(_){MEMORY_GRAPH.fullscreenFallback=true;}}
  else MEMORY_GRAPH.fullscreenFallback=true;graphSyncFullscreen();
}
function leaveMemoryGraph() {
  ++MEMORY_GRAPH.generation;MEMORY_GRAPH.controller?.abort();clearTimeout(MEMORY_GRAPH.timer);MEMORY_GRAPH.scene?.cleanup?.();graphExitFullscreen();
  const button=document.querySelector('#btn-graph-refresh');if(button)button.disabled=false;
}
function bindMemoryGraph() {
  if(MEMORY_GRAPH.bound)return;MEMORY_GRAPH.bound=true;graphSyncFullscreen();
  const on=(id,event,fn)=>document.querySelector(id)?.addEventListener(event,fn);
  on('#btn-graph-refresh','click',loadMemoryGraph);
  ['#graph-platform','#graph-person','#graph-phase','#graph-history','#graph-limit'].forEach(id=>on(id,'change',()=>{
    if(id==='#graph-phase'&&document.querySelector(id).value==='dormant')document.querySelector('#graph-history').checked=true;
    MEMORY_GRAPH.zoom=1;MEMORY_GRAPH.pan={x:0,y:0};loadMemoryGraph();
  }));
  on('#graph-search','input',()=>{++MEMORY_GRAPH.generation;MEMORY_GRAPH.controller?.abort();clearTimeout(MEMORY_GRAPH.timer);graphResetData();document.querySelector('#graph-canvas').replaceChildren(graphElement('p','正在匹配本地关联…','graph-empty'));document.querySelector('#graph-node-list').replaceChildren();renderGraphDetails();MEMORY_GRAPH.timer=setTimeout(loadMemoryGraph,250);});
  on('#graph-zoom-in','click',()=>graphZoom(1.25));on('#graph-zoom-out','click',()=>graphZoom(.8));on('#graph-fit','click',graphFit);
  on('#graph-details-close','click',()=>graphSelect(''));on('#graph-clear-selection','click',()=>graphSelect(''));
  on('#graph-mode-global','click',()=>{MEMORY_GRAPH.mode='global';graphPresentationChanged();});
  on('#graph-mode-local','click',()=>{
    if(!MEMORY_GRAPH.selected){document.querySelector('#graph-status').textContent='先选择一个节点，再以它为中心查看局部关联。';return;}
    MEMORY_GRAPH.localRoot=MEMORY_GRAPH.selected;MEMORY_GRAPH.mode='local';graphPresentationChanged();
  });
  on('#graph-depth','change',()=>{MEMORY_GRAPH.depth=Math.max(1,Math.min(3,Number(document.querySelector('#graph-depth').value)||1));graphPresentationChanged();});
  Object.keys(GRAPH_TYPES).forEach(type=>on('#graph-show-'+type,'change',()=>{if(document.querySelector('#graph-show-'+type).checked)MEMORY_GRAPH.types.add(type);else MEMORY_GRAPH.types.delete(type);graphPresentationChanged();}));
  on('#graph-label-density','input',()=>{MEMORY_GRAPH.density=Math.max(0,Math.min(2,Number(document.querySelector('#graph-label-density').value)||0));graphLabels();});
  on('#graph-node-scale','input',()=>{MEMORY_GRAPH.nodeScale=Math.max(.7,Math.min(1.6,Number(document.querySelector('#graph-node-scale').value)||1));const scene=MEMORY_GRAPH.scene;
    scene?.positions.forEach(n=>{n.r=graphNodeRadius(n.degree,MEMORY_GRAPH.nodeScale);const entry=scene.nodes.get(n.id);entry.group.children[0]?.setAttribute('r',Math.max(14,n.r+5));entry.group.children[1]?.setAttribute('r',n.r);});graphLabels();});
  on('#graph-settings','toggle',renderGraphDetails);on('#graph-fullscreen','click',graphToggleFullscreen);
  document.addEventListener?.('fullscreenchange',graphSyncFullscreen);
  document.addEventListener?.('keydown',event=>{if(event.key==='Escape'&&MEMORY_GRAPH.fullscreenFallback)graphExitFullscreen();});
  if(typeof ResizeObserver!=='undefined'){
    MEMORY_GRAPH.resizeObserver=new ResizeObserver(()=>{if(MEMORY_GRAPH.data&&document.querySelector('#page-memory-graph')?.classList.contains('active')){const dim=graphDimensions(),scene=MEMORY_GRAPH.scene;if(!scene||dim.width!==scene.width||dim.height!==scene.height)renderMemoryGraph();}});MEMORY_GRAPH.resizeObserver.observe(document.querySelector('#graph-canvas'));
  }
}
if(typeof module!=='undefined'&&module.exports)module.exports={graphLayout,graphNeighborhood,graphLocalData,graphFilterTypes,graphNodeRadius,graphZoomAt,graphLabelPlan};

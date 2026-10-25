'use strict';
let socket,lastSeq=0,retryTimer,closed=false,topmost=true,renderer,personaSpace='';
const bubbles=new Map(),pending=new Map(),ids=new Set();
const byId=id=>document.getElementById(id);
function bubble(id,text,user=false,draft=false){
  let node=bubbles.get(id);if(!node){node=document.createElement('div');node.className='bubble';bubbles.set(id,node);byId('conversation').appendChild(node);}
  node.classList.toggle('user',user);node.classList.toggle('draft',draft);node.textContent=text;
  byId('conversation').scrollTop=byId('conversation').scrollHeight;return node;
}
function receive(event){
  if(event.type==='space_changed'){
    if(changeSpace(event.persona_space))syncCurrentSpace();
    return;
  }
  if(typeof event.persona_space==='string'&&event.persona_space!==personaSpace)return;
  if(event.seq&&ids.has(event.seq))return;
  if(event.seq){ids.add(event.seq);lastSeq=Math.max(lastSeq,event.seq);if(ids.size>4096)ids.delete(ids.values().next().value);}
  if(event.type==='accepted'){bubble('user:'+event.client_id,event.text,true);pending.delete(event.client_id);}
  if(event.type==='delta'){const old=bubbles.get(event.turn_id)?.textContent||'';bubble(event.turn_id,old+event.text,false,true);byId('state').textContent='正在回应';if(renderer)renderer.state='responding';}
  if(event.type==='completed'){bubble(event.turn_id,event.text);byId('state').textContent='在这里';if(renderer)renderer.state='idle';}
  if(event.type==='cancelled'){bubbles.get(event.turn_id)?.remove();bubbles.delete(event.turn_id);byId('state').textContent='在听你说';}
  if(event.type==='state'){byId('state').textContent=event.state==='thinking'?'想一想…':'在这里';if(renderer)renderer.state=event.state;}
  if(event.type==='error'){byId('error').textContent=event.message||'暂时未完成，请稍后重试';}
}
function changeSpace(space){
  if(typeof space!=='string'||space===personaSpace)return false;
  personaSpace=space;lastSeq=0;ids.clear();pending.clear();
  for(const node of bubbles.values())node.remove();bubbles.clear();
  byId('text').value='';byId('error').textContent='';byId('state').textContent='在这里';
  if(renderer)renderer.state='idle';return true;
}
function syncCurrentSpace(){
  if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({type:'sync',after:0,persona_space:personaSpace}));
}
function connect(){
  if(closed)return;socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/companion`);
  socket.onopen=()=>{lastSeq=0;ids.clear();for(const [id,node] of bubbles){if(node.classList.contains('draft')){node.remove();bubbles.delete(id);}}byId('state').textContent='在这里';byId('error').textContent='';syncCurrentSpace();for(const value of pending.values()){if(value.persona_space===personaSpace)socket.send(JSON.stringify(value));}};
  socket.onmessage=event=>{try{receive(JSON.parse(event.data));}catch(_){byId('error').textContent='收到无效的会话事件';}};
  socket.onclose=event=>{byId('state').textContent=event.code===1008?'请在主界面启用伙伴并配置模型':'连接已断开，正在重连…';if(event.code!==1008&&!closed)retryTimer=setTimeout(connect,2000);};
}
byId('chat').addEventListener('submit',event=>{event.preventDefault();const text=byId('text').value;if(!text.trim())return;
  const client_id=crypto.randomUUID(),message={type:'message',client_id,text,persona_space:personaSpace};pending.set(client_id,message);bubble('user:'+client_id,text,true);byId('text').value='';
  if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify(message));else byId('error').textContent='连接恢复后会发送这条消息';
});
byId('text').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();byId('chat').requestSubmit();}});
byId('hide').onclick=()=>window.wxbotDesktop?.companion.hide();
byId('top').onclick=()=>window.wxbotDesktop?.companion.top(topmost=!topmost);
byId('through').onclick=()=>window.wxbotDesktop?.companion.through(true);
document.addEventListener('visibilitychange',()=>{if(renderer){if(document.hidden)renderer.pause();else renderer.resume();}});
window.addEventListener('beforeunload',()=>{closed=true;clearTimeout(retryTimer);socket?.close();renderer?.dispose();});
window.addEventListener('resize',()=>renderer?.resize());
async function init(){
 const response=await fetch('/api/companion/status');if(!response.ok)throw Error('请先在主界面登录');const status=await response.json();personaSpace=status.persona_space||'';connect();
 const model=status.models.find(m=>m.id===status.model_id);if(model&&window.WxBotLive2D){
  renderer=new WxBotLive2D.Renderer(byId('avatar'));
  renderer.onError=error=>{byId('fallback').hidden=false;byId('fallback').style.display='';byId('error').textContent='形象显示中断，文字聊天仍可使用：'+error.message;};
  try{await renderer.load(new URL('/api/companion/models/'+model.id+'/'+model.manifest,location.href).href);byId('fallback').hidden=true;byId('fallback').style.display='none';}
  catch(error){renderer.dispose();byId('error').textContent='形象读取未完成，文字聊天仍可使用：'+error.message;}
 }
}
init().catch(error=>{byId('error').textContent=error.message;});

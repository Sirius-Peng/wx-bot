"""Windows packaged API/resources smoke and repeated startup measurements (offline)."""
import argparse,json,os,secrets,socket,subprocess,tempfile,time,statistics,zipfile
from pathlib import Path
import httpx

def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def check(binary,output,runs=3):
 timings=[];records=[]
 with tempfile.TemporaryDirectory(prefix='wxbot 后端 中文 ') as d:
  data=Path(d);token=secrets.token_hex(24);messages=[{'ts':int(time.time()*1000),'direction':'in','chat_type':'private','chat_id':'fixture','chat_name':'测试','sender':'测试','sender_id':'fixture','content':'音乐 音乐 咖啡','platform':p} for p in ('ilink','onebot')]
  (data/'messages.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in messages),encoding='utf-8')
  (data/'config.json').write_text(json.dumps({'adapter':{'wechat_enabled':False,'qq_enabled':False},'llm':{'primary_key':''}}),encoding='utf-8')
  for i in range(runs):
   number=port();base=f'http://127.0.0.1:{number}';env={**os.environ,'WXBOT_DATA_DIR':d,'WXBOT_ADMIN_TOKEN':token,'WXBOT_HOST':'127.0.0.1','WXBOT_PORT':str(number),'WXBOT_SKIP_NAPCAT_SYNC':'1','WXBOT_MIGRATE_LEGACY':'0','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
   with (output.parent/(output.stem+f'-{i}.log')).open('w',encoding='utf-8') as log,httpx.Client(base_url=base,timeout=8,trust_env=False) as api:
    start=time.perf_counter();p=subprocess.Popen([str(binary)],env=env,stdout=log,stderr=log,cwd=binary.parent)
    try:
     deadline=time.monotonic()+60
     while time.monotonic()<deadline:
      assert p.poll() is None,'frozen backend exited'
      try:
       if api.get('/healthz').status_code==200:break
      except httpx.RequestError:pass
      time.sleep(.05)
     else:raise AssertionError('backend readiness timed out')
     timings.append(round((time.perf_counter()-start)*1000,2))
     assert api.get('/api/config').status_code==401
     api.headers['Authorization']='Bearer '+token
     stats=api.get('/api/statistics?min_count=1').json();assert stats['summary']['messages']==2,stats
     assert next(w for w in stats['words'] if w['label']=='音乐')['count']==4
     assert api.get('/api/statistics?platform=qq').json()['summary']['messages']==1
     assert api.get('/api/data/status').status_code==200
     assert api.get('/api/onboarding').status_code==200
     assert '对话统计' in api.get('/').text
     assert api.get('/static/statistics.js').status_code==200
     backup=api.post('/api/data/backup').json();assert backup['ok'],backup
     downloaded=api.get(backup['download_url']);assert downloaded.status_code==200
     with zipfile.ZipFile(Path(backup['path'])) as z:assert 'messages.jsonl' in z.namelist()
     if i==0:assert api.post('/api/memory',json={'scope':'user','owner_id':'fixture','category':'fact','content':'重启后仍存在的测试记忆','keywords':['测试']}).json()['ok']
     assert api.get('/api/memory/stats').json()['total']==1
     assert api.post('/api/app/shutdown').status_code==200;p.wait(timeout=20);assert p.returncode==0
     with socket.socket() as s:assert s.connect_ex(('127.0.0.1',number))!=0,'backend listener remained open'
     records.append({'iteration':i,'api_resources':'PASS','exit_status':0})
    finally:
     if p.poll() is None:subprocess.run(['taskkill.exe','/PID',str(p.pid),'/T','/F'],capture_output=True);p.wait(timeout=10)
 result={'binary':str(binary),'startup_ms':timings,'median_ms':round(statistics.median(timings),2),'records':records,'summary':f'WINDOWS_BACKEND_SMOKE PASS runs={runs} auth=1 statistics=1 jieba=1 timezone=1 onboarding=1 backup=1 memory_retained=1 shutdown=1','exit_status':0}
 output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(result['summary']);print('STARTUP_MS '+json.dumps(timings)+' median='+str(result['median_ms']))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--binary',required=True);p.add_argument('--report',required=True);p.add_argument('--runs',type=int,default=3);a=p.parse_args();check(Path(a.binary).resolve(),Path(a.report).resolve(),a.runs)

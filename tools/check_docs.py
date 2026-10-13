"""Offline documentation consistency checks; no runtime/config/model writes."""
from __future__ import annotations
import argparse,ast,hashlib,json,re,sys
from pathlib import Path
from urllib.parse import unquote,urlsplit


def audit(root,metadata=None):
    errors=[];checks=0
    def check(ok,message):
        nonlocal checks
        checks+=1
        if not ok:errors.append(message)
    pkg=json.loads((root/'desktop/package.json').read_text());version=pkg['version']
    readme=(root/'README.md').read_text();desktop=(root/'desktop/README.md').read_text()
    manifest_path=root/f'docs/release-v{version}.json'
    check(manifest_path.exists(),'missing published release manifest')
    manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    check(manifest.get('version')==version,'manifest/package version mismatch')
    check(manifest.get('tag')=='v'+version,'manifest tag mismatch')
    check(f'当前发布：v{version}' in readme,'README current version missing')
    check('v'+version in desktop.splitlines()[0],'desktop header version missing')
    for f in ['CHANGELOG.md','docs/README.md','docs/validation.md']:
        check((root/f).is_file(),'missing documentation: '+f)
    desired={'WxBot-'+version+'-Windows-x64-Setup.exe','WxBot-'+version+'-Windows-x64-Portable.exe','WxBot-'+version+'-macOS-arm64.dmg','SHA256SUMS.txt'}
    names={a['name'] for a in manifest.get('artifacts',[])}
    check(names==desired,'release asset manifest mismatch')
    for a in manifest.get('artifacts',[]):
        check(bool(re.fullmatch('[0-9a-f]{64}',a.get('sha256',''))),'invalid sha256: '+a['name'])
        check(isinstance(a.get('bytes'),int) and a['bytes']>0,'invalid size: '+a['name'])
        check(a.get('url')=='https://github.com/Sirius-Peng/wx-bot/releases/download/v'+version+'/'+a['name'],'download URL mismatch: '+a['name'])
        check(a['name'] in readme,'README missing asset: '+a['name'])
    source=(root/'src/config_store.py').read_text()
    for name,value in [('primary_base','https://api.deepseek.com'),('primary_model','deepseek-flash'),('context_timezone','Asia/Shanghai')]:
        check(bool(re.search(r'\b'+name+r': str = "'+re.escape(value)+r'"',source)),'default source changed: '+name)
        check(value in readme,'README missing model/time default: '+name)
    for name,value in [('message_merge_window',2.0),('message_merge_max_wait',10.0),('message_pause_timeout',120.0)]:
        check(bool(re.search(r'\b'+name+r': float = '+str(value),source)),'default waiting configuration changed: '+name)
    for name in ['Windows','macOS','Linux','user-data','XDG_DATA_HOME','LOCALAPPDATA']:
        check(name in readme and name in desktop,'platform data paths incomplete: '+name)
    for old in ['零封号','39 项测试','五层触发漏斗','10 元够用','成本可降到 0','存在即成功']:
        check(old not in readme+desktop,'obsolete claim: '+old)
    html=(root/'src/web/index.html').read_text()
    for id,label in [('overview','概览'),('connect','接入设置'),('memory','人格知识库'),('messages','消息记录'),('statistics','对话统计'),('settings','模型与行为'),('guide','上手引导')]:
        check('id="page-'+id+'"' in html and label in readme,'page docs mismatch: '+id)
    sections=re.findall(r'data-settings-section="([^"]+)"',html)
    check(len(set(sections)-{'channels'})==8,'settings section count changed; update NapCat docs')
    check('八个配置卡片' in (root/'docs/napcat-fixes.md').read_text(),'settings card docs mismatch')
    check("'--onedir'" in (root/'desktop/scripts/build-backend.ps1').read_text(encoding='utf-8-sig') and 'onedir' in desktop,'Windows layout mismatch')
    check('--onefile' in (root/'desktop/scripts/build-backend.sh').read_text() and 'onefile' in desktop,'macOS layout mismatch')
    paths=[root/'README.md',root/'CHANGELOG.md',root/'desktop/README.md',*sorted((root/'docs').glob('*.md'))]
    local_links=0
    for p in paths:
        if not p.exists():continue
        text=re.sub(r'```[\s\S]*?```','',p.read_text())
        for target in re.findall(r'\]\(([^\s)]+)(?:\s+[^)]*)?\)',text):
            u=urlsplit(target.strip('<>'))
            if u.scheme or target.startswith('#'):continue
            dest=(p.parent/unquote(u.path)).resolve();local_links+=1
            check(dest.is_file() or dest.is_dir(),f'broken local link: {p.relative_to(root)} -> {target}')
    if metadata is not None:
        description=metadata.get('description','');topics={t['name'] if isinstance(t,dict) else t for t in metadata.get('repositoryTopics',[])}
        check('微信私聊' in description and 'QQ' in description,'repository About channels mismatch')
        check('GeWe' not in description and '智能插话' not in description,'repository About legacy claims')
        check(metadata.get('homepageUrl')=='https://github.com/Sirius-Peng/wx-bot/releases/latest','repository homepage mismatch')
        check('gewechat' not in topics and {'electron','onebot','napcat','long-term-memory'}<=topics,'repository topics mismatch')
    return {'version':version,'checks':checks,'local_links':local_links,'errors':errors,'ok':not errors}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));p.add_argument('--metadata');p.add_argument('--report');a=p.parse_args()
    result=audit(Path(a.root).resolve(),json.loads(Path(a.metadata).read_text()) if a.metadata else None)
    if a.report:Path(a.report).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if result['ok']:print(f"DOCS PASS version={result['version']} checks={result['checks']} local_links={result['local_links']} errors=0")
    else:
        print('DOCS FAIL '+json.dumps(result,ensure_ascii=False));sys.exit(1)

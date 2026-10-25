"""Stable persona/relationship namespaces. The original data root never moves.

Only an enumerated pair selects a path. Login, transport drafts, contact clocks,
explicit forgetting and account links remain global controls, not persona facts.
"""
from __future__ import annotations
import json
from pathlib import Path
from .personality import PROFILE_IDS,RELATIONSHIP_IDS
from .storage import _atomic_json,_snapshot,_lock


def space_key(profile,relationship):
    if not isinstance(profile,str) or not isinstance(relationship,str) or profile not in PROFILE_IDS or relationship not in RELATIONSHIP_IDS:
        raise ValueError('人格或关系模式无效')
    return profile+'--'+relationship


def validate_state(raw):
    if not isinstance(raw,dict) or type(raw.get('version')) is not int or raw.get('version')!=1:
        raise ValueError('人格空间索引格式无效')
    for field in ('legacy_space','active_space'):
        value=raw.get(field)
        if not isinstance(value,str) or value.count('--')!=1:
            raise ValueError('人格空间标识无效')
        if space_key(*value.split('--'))!=value:raise ValueError('人格空间标识无效')
    return raw


class PersonaSpaces:
    def __init__(self,root,bot):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'persona-state.json'
        if self.path.is_symlink():raise ValueError('人格空间索引不能是符号链接')
        if self.path.exists():self.state=validate_state(json.loads(self.path.read_text(encoding='utf-8')))
        else:
            folder=self.root/'persona-spaces'
            if folder.exists() and any(folder.iterdir()):raise ValueError('人格空间索引缺失，请从备份恢复')
            key=space_key(bot.persona_profile,bot.persona_relationship)
            self.state={'version':1,'legacy_space':key,'active_space':key}
            with _lock(self.root):
                if self.path.exists():self.state=validate_state(json.loads(self.path.read_text(encoding='utf-8')))
                else:
                    if (self.root/'config.json').exists() or (self.root/'memory.json').exists():
                        backup=_snapshot(self.root,self.root/'.backups','persona-spaces-v3')
                        self.state['upgrade_backup']=str(backup.relative_to(self.root))
                    _atomic_json(self.path,self.state)
        requested=space_key(bot.persona_profile,bot.persona_relationship)
        self.active_key=self.state['active_space']
        self.prepare_shared_controls()
        previous=self.read_settings(self.active_key)
        restore=requested!=self.active_key and previous is not None and (previous['name'],previous['persona'])==(bot.name,bot.persona)
        self.activate(bot,restore_existing=restore)

    @property
    def legacy_key(self):return self.state['legacy_space']

    def directory(self,key=None):
        key=key or self.active_key
        if not isinstance(key,str) or key.count('--')!=1:raise ValueError('人格空间标识无效')
        space_key(*key.split('--'))
        path=self.root if key==self.legacy_key else self.root/'persona-spaces'/key
        current=path
        while current!=self.root:
            if current.is_symlink():raise ValueError('人格空间不能指向外部目录')
            current=current.parent
        if not path.resolve().is_relative_to(self.root):raise ValueError('人格空间路径无效')
        path.mkdir(parents=True,exist_ok=True)
        return path

    def read_settings(self,key):
        path=self.directory(key)/'persona.json'
        if not path.exists():return None
        if path.is_symlink():raise ValueError('人格配置不能是符号链接')
        raw=json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(raw,dict) or raw.get('schema')!='wxbot.persona-settings.v1'
                or not isinstance(raw.get('name'),str) or not isinstance(raw.get('persona'),str)
                or space_key(raw.get('persona_profile'),raw.get('persona_relationship'))!=key):
            raise ValueError('persona.json已有未知用户内容或空间错位，原文件已保留')
        return raw

    def activate(self,bot,*,restore_existing=False):
        key=space_key(bot.persona_profile,bot.persona_relationship)
        directory=self.directory(key)
        existing=self.read_settings(key)
        if restore_existing and existing is not None:
            bot.name=existing['name'];bot.persona=existing['persona']
        # Configuration stays outside memory.json; config.json remains a compatible mirror.
        settings={'schema':'wxbot.persona-settings.v1','version':1,'persona_profile':bot.persona_profile,'persona_relationship':bot.persona_relationship,
                  'name':bot.name,'persona':bot.persona}
        path=directory/'persona.json'
        if path.is_symlink():raise ValueError('人格配置不能是符号链接')
        if not path.exists() or existing!=settings:_atomic_json(path,settings)
        state={**self.state,'active_space':key}
        if state!=self.state:_atomic_json(self.path,state)
        self.state=state;self.active_key=key
        return self.info()

    def info(self):
        root=self.directory()
        return {'id':self.active_key,'legacy_id':self.legacy_key,
                'profile':self.active_key.split('--')[0],'relationship':self.active_key.split('--')[1],
                'memory_file':str((root/'memory.json').relative_to(self.root)),
                'persona_file':str((root/'persona.json').relative_to(self.root)),
                'selection_file':'persona-state.json'}

    def prepare_shared_controls(self):
        """Extract only upgrade-era controls, never copy legacy facts to a new role."""
        controls=[self.root/'identity-links.json',self.root/'memory-forgotten.json']
        if any(p.is_symlink() for p in controls):raise ValueError('共享控制文件不能是符号链接')
        if all(p.exists() for p in controls):return
        original=self.root/'memory.json'
        raw=json.loads(original.read_text(encoding='utf-8')) if original.exists() else {}
        if not isinstance(raw,dict):raise ValueError('原记忆文件需要恢复')
        for name,field,source in [('identity-links.json','identities','identities'),('memory-forgotten.json','markers','suppressions')]:
            path=self.root/name
            if path.is_symlink():raise ValueError('共享控制文件不能是符号链接')
            if not path.exists():
                values=raw.get(source,[])
                if not isinstance(values,list):raise ValueError('共享控制格式无效')
                _atomic_json(path,{'version':1,field:values})

    def context_paths(self):
        paths=list(self.root.glob('context-*.json'))
        parent=self.root/'persona-spaces'
        if parent.exists():
            for child in parent.iterdir():
                if child.is_symlink() or not child.is_dir():continue
                try:space_key(*child.name.split('--'))
                except (TypeError,ValueError):continue
                paths.extend(child.glob('context-*.json'))
        return [p for p in paths if not p.is_symlink() and p.resolve().is_relative_to(self.root)]

    def memory_paths(self):
        paths=[self.root/'memory.json',*(self.root/'persona-spaces').glob('*/memory.json')]
        result=[]
        for path in paths:
            if not path.exists() or path.is_symlink() or path.parent.is_symlink():continue
            if path.parent!=self.root:
                try:space_key(*path.parent.name.split('--'))
                except (TypeError,ValueError):continue
            if path.resolve().is_relative_to(self.root):result.append(path)
        return result

"""Atomic local model imports. Resources are served only from validated manifests."""
from __future__ import annotations
import json,os,shutil,tempfile,uuid
from pathlib import Path,PurePosixPath
from urllib.parse import urlsplit

class CompanionModels:
    def __init__(self,data_dir):self.root=Path(data_dir)/'companion'/'models'

    @staticmethod
    def references(manifest):
        refs=manifest.get('FileReferences',{})
        names=[]
        for key in ('Moc','Physics','Pose','DisplayInfo','UserData'):
            if refs.get(key):names.append(refs[key])
        names+=refs.get('Textures',[])
        names += [e['File'] for e in refs.get('Expressions',[]) if isinstance(e,dict) and e.get('File')]
        names += [e['File'] for group in refs.get('Motions',{}).values() for e in group if isinstance(e,dict) and e.get('File')]
        if not refs.get('Moc') or not refs.get('Textures'):raise ValueError('模型缺少Moc或纹理引用')
        return list(dict.fromkeys(names))

    @staticmethod
    def checked(root,relative):
        if not isinstance(relative,str) or not relative or '\\' in relative or urlsplit(relative).scheme or relative.startswith('/'):
            raise ValueError('模型资源必须为目录内的相对文件')
        parts=PurePosixPath(relative).parts
        if '..' in parts or ':' in relative:raise ValueError('模型资源路径无效')
        path=root.joinpath(*parts)
        if any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(root)):
            raise ValueError('模型不能包含符号链接')
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():raise ValueError('模型资源缺失')
        return path

    def import_model(self,source,manifest=''):
        root=Path(source).expanduser()
        if root.is_symlink() or not root.is_dir():raise ValueError('请选择本地模型目录')
        choices=[p.relative_to(root).as_posix() for p in root.rglob('*.model3.json') if not p.is_symlink()]
        if not manifest:
            if len(choices)!=1:return {'needs_selection':True,'manifests':choices[:100]}
            manifest=choices[0]
        entry=self.checked(root,manifest)
        if entry.stat().st_size>2*1024*1024:raise ValueError('模型清单过大')
        raw=json.loads(entry.read_text(encoding='utf-8-sig'))
        refs=self.references(raw);parent=entry.parent
        files=[(entry,manifest)]
        for ref in refs:
            file=self.checked(parent,ref)
            files.append((file,file.relative_to(root).as_posix()))
        if len(files)>1000 or sum(p.stat().st_size for p,_ in files)>256*1024*1024:raise ValueError('模型超过256MiB或1000文件上限')
        self.root.mkdir(parents=True,exist_ok=True)
        temp=Path(tempfile.mkdtemp(prefix='.import-',dir=self.root));mid=uuid.uuid4().hex
        try:
            for file,relative in files:
                target=temp/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,target)
            record={'id':mid,'name':entry.stem.replace('.model3',''),'manifest':manifest,'resources':[r for _,r in files]}
            (temp/'import.json').write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
            os.replace(temp,self.root/mid)
            return record
        finally:
            if temp.exists():shutil.rmtree(temp)

    def describe(self,mid):
        if not isinstance(mid,str) or len(mid)!=32 or any(c not in '0123456789abcdef' for c in mid):raise ValueError('模型编号无效')
        root=self.root/mid
        if root.is_symlink():raise ValueError('模型路径无效')
        return json.loads((root/'import.json').read_text(encoding='utf-8'))

    def resource(self,mid,relative):
        record=self.describe(mid)
        if relative not in record['resources']:raise ValueError('资源不在模型清单中')
        return self.checked(self.root/mid,relative)

    def models(self):
        if not self.root.exists():return []
        result=[]
        for path in self.root.iterdir():
            try:result.append(self.describe(path.name))
            except (OSError,ValueError):continue
        return result

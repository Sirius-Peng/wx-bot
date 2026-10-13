"""Stable local user data, non-destructive legacy migration and verified ZIP snapshots.

Application binaries / Electron cache are disposable. This directory is not.
Explicit path overrides remain supported; migration is opt-in for isolated paths.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
import zipfile

MARKER = '.storage-migration.json'
PENDING = '.storage-migration.pending.json'
MANIFEST = '__wxbot_backup_manifest__.json'
SKIP_DIRS = {'.backups', '__pycache__', '.git'}


def resolve_data_dir(platform=None, home=None, env=None):
    platform = platform or sys.platform; home = Path(home or Path.home()); env = os.environ if env is None else env
    if env.get('WXBOT_DATA_DIR'): return Path(env['WXBOT_DATA_DIR']).expanduser().resolve()
    if env.get('WXBOT_USER_DATA_DIR'): return (Path(env['WXBOT_USER_DATA_DIR']).expanduser()/'data').resolve()
    if platform == 'darwin': root = home/'Library'/'Application Support'/'WxBot'
    elif platform.startswith('win'): root = Path(env.get('LOCALAPPDATA') or home/'AppData'/'Local')/'WxBot'
    else:
        base = env.get('XDG_DATA_HOME','')
        root = (Path(base) if base and Path(base).is_absolute() else home/'.local'/'share')/'wxbot'
    return (root/'user-data').resolve()


def legacy_data_dirs(env=None, home=None, platform=None):
    env = os.environ if env is None else env;home = Path(home or Path.home());platform = platform or sys.platform
    out=[]
    if env.get('WXBOT_LEGACY_DATA_DIRS'):
        supplied=json.loads(env['WXBOT_LEGACY_DATA_DIRS'])
        if not isinstance(supplied,list) or not all(isinstance(p,str) for p in supplied):raise ValueError('Legacy directories must be a JSON list of paths')
        return list(dict.fromkeys(Path(p).expanduser().resolve() for p in supplied))
    if platform=='darwin':base=home/'Library'/'Application Support'
    elif platform.startswith('win'):base=Path(env.get('APPDATA') or home/'AppData'/'Roaming')
    else:base=Path(env.get('XDG_CONFIG_HOME') or home/'.config')
    repo=Path(__file__).resolve().parents[2]
    out.append(repo/'data')
    if (Path.cwd()/'src'/'server.py').is_file():out.append(Path.cwd()/'data')
    out += [base/name/'data' for name in ('微信AI助手','微信 AI 助手','wxbot-desktop','wxbot','WxBot')]
    return list(dict.fromkeys(p.resolve() for p in out))


def _atomic_json(path, value):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.storage-',suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)


@contextmanager
def _lock(root):
    lock=root/'.storage.lock';fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'r+b') as f:
        # Windows can lock a byte beyond EOF. Never initialize before locking:
        # concurrent first starts may both see an empty file while one holds its byte.
        deadline=time.monotonic()+10
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except (OSError,BlockingIOError):
                if time.monotonic()>deadline:raise RuntimeError('User data is busy; retry after the other startup completes')
                time.sleep(.05)
        try:
            if os.fstat(f.fileno()).st_size==0:
                f.seek(0);f.write(b'0');f.flush()
            yield
        finally:
            if os.name=='nt':
                import msvcrt
                f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def _payload_files(root):
    if root.is_symlink():raise ValueError('Data directory symlinks are not included in snapshots')
    for folder,dirs,files in os.walk(root,followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.migration-'))
        for name in dirs:
            if (Path(folder)/name).is_symlink():raise ValueError('Data contains a linked directory; snapshot stopped')
        for name in sorted(files):
            if name=='.gitkeep' or name==MANIFEST or name.startswith('.storage') or name.endswith('.tmp'):continue
            p=Path(folder)/name
            if p.is_symlink() or not p.is_file():raise ValueError('Data contains a linked or non-regular file; snapshot stopped')
            yield p,p.relative_to(root).as_posix()


def verify_backup(file):
    with zipfile.ZipFile(file) as z:
        m=json.loads(z.read(MANIFEST));expected=m['files']
        if set(z.namelist()) != set(expected)|{MANIFEST}:raise ValueError('Backup members do not match manifest')
        for name,digest in expected.items():
            if hashlib.sha256(z.read(name)).hexdigest()!=digest:raise ValueError('Backup hash verification failed')
    return m


def _snapshot(root, destination, kind='backup', extras=None):
    if destination.is_symlink():raise ValueError('Backup directory must not be a symbolic link')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final=destination/f'wxbot-{kind}-{stamp}-{uuid.uuid4().hex[:8]}.zip'
    fd,tmp=tempfile.mkstemp(dir=destination,prefix='.storage-',suffix='.tmp');os.close(fd)
    hashes={};origins={'data':str(root)}
    try:
        with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
            sets=[(root,'')]
            for label,extra in (extras or {}).items():
                extra=Path(extra).resolve()
                if not extra.is_relative_to(root.resolve()) and extra.is_dir():
                    sets.append((extra,'external/'+label+'/'));origins[label]=str(extra)
            for source,prefix in sets:
                for p,relative in _payload_files(source):
                    before=p.stat();digest=hashlib.sha256();name=prefix+relative
                    with p.open('rb') as f,z.open(name,'w',force_zip64=True) as out:
                        for chunk in iter(lambda:f.read(1024*1024),b''):out.write(chunk);digest.update(chunk)
                    after=p.stat()
                    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise RuntimeError('Data changed during snapshot; retry')
                    hashes[name]=digest.hexdigest()
            z.writestr(MANIFEST,json.dumps({'version':1,'created_at':stamp,'origins':origins,'files':hashes},ensure_ascii=False))
        os.chmod(tmp,0o600);verify_backup(tmp);os.replace(tmp,final)
        return final
    finally:Path(tmp).unlink(missing_ok=True)


def create_backup(data_dir, extras=None):
    root=Path(data_dir).resolve()
    with _lock(root):
        for attempt in range(3):
            try:return _snapshot(root,root/'.backups',extras=extras)
            except RuntimeError:
                if attempt==2:raise


def _has_state(root):
    return any((root/f).is_file() for f in ('config.json','memory.json','onboarding.json','messages.jsonl','qq-bursts.json','ilink/account.json')) or any(root.glob('context-*.json'))


def _finish_pending(root,pending):
    name=pending['archive']
    if Path(name).name!=name or not name.startswith('wxbot-migration-'):raise ValueError('Invalid migration backup name')
    file=root/'.backups'/name;manifest=verify_backup(file)
    imported={name:digest for name,digest in manifest['files'].items() if Path(name).parts[0]!='logs'}
    with zipfile.ZipFile(file) as z:
        # Check every path and conflict before writing. Existing newer files always win.
        for name,digest in imported.items():
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Invalid migration member')
            target=root/relative
            if not target.resolve().is_relative_to(root):raise ValueError('Migration destination contains a linked path')
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise RuntimeError('Migration conflict: existing data retained; inspect local migration backup')
        for name,digest in imported.items():
            target=root/name
            if target.exists():continue
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            fd,tmp=tempfile.mkstemp(dir=target.parent,prefix='.storage-',suffix='.tmp')
            try:
                with os.fdopen(fd,'wb') as out:out.write(z.read(name));out.flush();os.fsync(out.fileno())
                os.chmod(tmp,0o600)
                # Do not replace a file created by another process during migration.
                try:os.link(tmp,target)
                except FileExistsError:
                    if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise RuntimeError('Concurrent migration conflict; existing data retained')
            finally:Path(tmp).unlink(missing_ok=True)
    report={'version':1,'status':'migrated','imported_files':len(imported),
            'source':pending['source'],'archive':pending['archive'],'other_backups':pending.get('other_backups',[])}
    _atomic_json(root/MARKER,report);(root/PENDING).unlink(missing_ok=True)
    return report


def initialize_storage(data_dir=None, sources=None, migrate=None):
    root=Path(data_dir or resolve_data_dir()).resolve();root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if migrate is None:
        setting=os.environ.get('WXBOT_MIGRATE_LEGACY')
        migrate=setting=='1' if setting is not None else not (os.environ.get('WXBOT_DATA_DIR') or os.environ.get('WXBOT_USER_DATA_DIR'))
    with _lock(root):
        if (root/PENDING).is_file():return _finish_pending(root,json.loads((root/PENDING).read_text()))
        if (root/MARKER).is_file():return json.loads((root/MARKER).read_text())
        if not migrate:return {'version':1,'status':'custom','imported_files':0}
        candidates=[]
        for p in (sources if sources is not None else legacy_data_dirs()):
            p=Path(p).resolve()
            if p==root or p.is_relative_to(root) or root.is_relative_to(p):continue
            if p.is_dir() and _has_state(p) and p not in candidates:candidates.append(p)
        if _has_state(root) or not candidates:
            report={'version':1,'status':'existing' if _has_state(root) else 'fresh','imported_files':0}
            _atomic_json(root/MARKER,report);return report
        archives=[_snapshot(p,root/'.backups','migration') for p in candidates]
        pending={'archive':archives[0].name,'source':str(candidates[0]),'other_backups':[p.name for p in archives[1:]]}
        _atomic_json(root/PENDING,pending)
        return _finish_pending(root,pending)


def migrated_state_path(data_dir, absolute_path):
    """Remap only an old path inside the selected migrated dataset; keep custom paths."""
    root=Path(data_dir).resolve();p=Path(absolute_path).expanduser().resolve()
    try:report=json.loads((root/MARKER).read_text())
    except (OSError,ValueError):return p
    if report.get('status')=='migrated' and report.get('source'):
        old=Path(report['source']).resolve()
        if p.is_relative_to(old):return root/p.relative_to(old)
    return p

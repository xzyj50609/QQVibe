"""Full data snapshots and offline, recoverable restore of QQVibe-owned files."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import ExitStack,closing
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath

from account_store import _check_root,_regular,_reparse
from qq_message_store import _validate_database,SCHEMA_VERSION

FORMAT='qqvibe-full-data-v1'
MAX_FILES=50000
MAX_BYTES=5*1024**3
CONFIGS={'qq-connection.json','inference-settings.json','local-model-source.json','model-source.json','api-model-source.json'}
PATHS=[re.compile(r'accounts/[0-9a-f]{32}/messages\.sqlite'),
       re.compile(r'accounts/[0-9a-f]{32}/(?:analysis-staging|api-analysis-staging)/[0-9a-f]{32}\.sqlite3'),
       re.compile(r'accounts/[0-9a-f]{32}/api-analysis-staging/(?:inventory|[0-9a-f]{32})/[0-9a-f]{32}\.sqlite3'),
       re.compile(r'real-client-data/[0-9a-f]{64}\.sqlite3')]


def allowed_file(value):
    return value=='real-client-data/accounts.json' or value in {'real-client-runtime/'+name for name in CONFIGS} or any(pattern.fullmatch(value) for pattern in PATHS)


def digest(file):
    value=hashlib.sha256()
    with Path(file).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def windows_user():
    if os.name!='nt':return 'non-windows-test-user'
    import win32api,win32con,win32security
    token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:sid=win32security.GetTokenInformation(token,win32security.TokenUser)[0]
    finally:token.Close()
    return hashlib.sha256(win32security.ConvertSidToStringSid(sid).encode()).hexdigest()


def checked_root(root):
    root=Path(os.path.abspath(root))
    _check_root(root)
    if root.exists() and (_reparse(root) or not root.is_dir()):raise ValueError('unsafe-data-directory')
    return root


def _sqlite_copy(source,target):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(target)) as copied:
        original.backup(copied)
        if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup-database-invalid')


def _validate_file(file,relative):
    if relative.endswith(('.sqlite','.sqlite3')):
        with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)) as conn:
            if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('backup-database-invalid')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' OR sql LIKE '%VIRTUAL TABLE%' LIMIT 1").fetchone():
                raise ValueError('backup-active-sql-rejected')
            if relative.endswith('/messages.sqlite'):_validate_database(conn,(SCHEMA_VERSION,))
    elif relative.endswith('.json'):
        if file.stat().st_size>2*1024*1024:raise ValueError('backup-json-too-large')
        value=json.loads(file.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError('backup-json-invalid')


def rebase_account_registry(prepared,destination):
    """Bind existing account records to the restored installation, never infer new identities."""
    from account_store import account_id
    from qq_identity import account_key,canonical_uin,is_account_key
    from qq_message_store import _stored_accounts
    prepared=checked_root(prepared);destination=checked_root(destination)
    registry=prepared/'real-client-data/accounts.json'
    if not registry.is_file():return
    value=json.loads(registry.read_text(encoding='utf-8'))
    if value.get('version')!=1 or not isinstance(value.get('accounts'),list):raise ValueError('backup-account-registry-invalid')
    seen=set()
    for item in value['accounts']:
        if not isinstance(item,dict) or not is_account_key(item.get('account')):raise ValueError('backup-account-registry-invalid')
        account=item['account'];identifier=account_id(account)
        if account in seen or item.get('accountId')!=identifier or account_key(canonical_uin(item.get('ownerUin')))!=account:
            raise ValueError('backup-account-registry-invalid')
        seen.add(account)
        database=prepared/'accounts'/account[2:]/'messages.sqlite'
        if not database.is_file() or _reparse(database):raise ValueError('backup-account-library-missing')
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as conn:
            stored=_stored_accounts(conn)
            if stored and stored!={account}:raise ValueError('backup-account-library-mismatch')
        item['workdir']=str(destination/'accounts'/account[2:])
    registry.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')


def _counts(directory):
    counts={'accounts':0,'messages':0,'labelCacheRows':0,'portraitCheckpoints':0}
    for file in directory.glob('accounts/*/messages.sqlite'):
        counts['accounts']+=1
        with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)) as conn:
            counts['messages']+=conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
    for file in directory.glob('real-client-data/*.sqlite3'):
        with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)) as conn:
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table,key in [('fine_results_v1','labelCacheRows'),('api_insights_v1','labelCacheRows'),
                              ('batch_progress_v1','portraitCheckpoints'),('api_portrait_v1','portraitCheckpoints')]:
                if table in tables:counts[key]+=conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    return counts


def create_backup(root,destination,*,app_version='0.1.0',lock_scope=None,ui_preferences=None):
    root=checked_root(root)
    destination=Path(os.path.abspath(destination))
    _check_root(destination.parent);_regular(destination)
    if destination.exists():raise FileExistsError('backup-already-exists')
    if destination.is_relative_to(root/'accounts') or destination.is_relative_to(root/'real-client-data') or destination.is_relative_to(root/'real-client-runtime'):
        raise ValueError('backup-destination-is-live-data')
    destination.parent.mkdir(parents=True,exist_ok=True)
    work=root/'backups'/('.snapshot-'+uuid.uuid4().hex)
    _check_root(work.parent);work.mkdir(parents=True)
    temporary=destination.with_name('.'+destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with ExitStack() as stack:
            if lock_scope is not None:stack.enter_context(lock_scope())
            candidates=[]
            for folder in ('accounts','real-client-data','real-client-runtime'):
                origin=root/folder
                if not origin.exists():continue
                for source in origin.rglob('*'):
                    if _reparse(source):raise ValueError('linked-backup-source')
                    if not source.is_file():continue
                    relative=source.relative_to(root).as_posix()
                    if allowed_file(relative):candidates.append((source,relative))
            if len(candidates)>MAX_FILES:raise ValueError('backup-file-limit')
            for source,relative in candidates:
                target=work/relative;target.parent.mkdir(parents=True,exist_ok=True)
                if relative.endswith(('.sqlite','.sqlite3')):_sqlite_copy(source,target)
                else:shutil.copy2(source,target)
        # All application locks end before hashing/compression of the snapshot.
        files=[]
        for file in sorted(work.rglob('*')):
            if not file.is_file():continue
            relative=file.relative_to(work).as_posix();_validate_file(file,relative)
            files.append({'path':relative,'bytes':file.stat().st_size,'sha256':digest(file)})
        if sum(row['bytes'] for row in files)>MAX_BYTES:raise ValueError('backup-size-limit')
        if not any(row['path'].endswith('/messages.sqlite') for row in files):raise ValueError('backup-has-no-message-library')
        manifest={'schema':FORMAT,'appVersion':app_version,'dataSchema':SCHEMA_VERSION,
            'createdUtc':datetime.now(timezone.utc).isoformat(),'windowsUserHash':windows_user(),
            'counts':_counts(work),'files':files,'uiPreferences':checked_preferences(ui_preferences or {}),
            'includes':['message-libraries','analysis-results','durable-checkpoints','session-selection','local-model-and-connection-settings'],
            'excludes':['model-weights','browser-cache','application-binaries','logs','older-backups']}
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
            for row in files:archive.write(work/row['path'],'data/'+row['path'])
        inspected=inspect_backup(temporary)
        if inspected['files']!=manifest['files']:raise ValueError('backup-verification-failed')
        _check_root(destination.parent);_regular(destination)
        os.link(temporary,destination)
        return {'state':'complete','path':str(destination),'bytes':destination.stat().st_size,
            'sha256':digest(destination),'createdUtc':manifest['createdUtc'],'counts':manifest['counts']}
    finally:
        temporary.unlink(missing_ok=True)
        # Preserve the snapshot as a private recovery point, including on failure.


def checked_preferences(value):
    if not isinstance(value,dict) or not set(value)<={'theme','zoom','intent','labelDetails'}:raise ValueError('invalid-ui-preferences')
    if 'theme' in value and value['theme'] not in ('dark','light'):raise ValueError('invalid-ui-preferences')
    if 'zoom' in value and value['zoom'] not in ('0.8','0.9','1.0','1.1','1.2','1.25','1.5','1.75','2.0'):raise ValueError('invalid-ui-preferences')
    if any(type(value[name]) is not bool for name in ('intent','labelDetails') if name in value):raise ValueError('invalid-ui-preferences')
    return dict(value)


def inspect_backup(file,*,extract_to=None):
    file=Path(os.path.abspath(file));_check_root(file.parent)
    if not _regular(file) or file.stat().st_size>MAX_BYTES:raise ValueError('backup-file-invalid')
    with zipfile.ZipFile(file) as archive:
        entries=archive.infolist()
        if len(entries)>MAX_FILES+1 or len({row.filename.casefold() for row in entries})!=len(entries):raise ValueError('backup-members-invalid')
        if sum(row.file_size for row in entries)>MAX_BYTES:raise ValueError('backup-size-limit')
        manifest_row=archive.getinfo('manifest.json')
        if manifest_row.file_size>8*1024*1024:raise ValueError('backup-manifest-too-large')
        manifest=json.loads(archive.read(manifest_row))
        if not isinstance(manifest,dict) or manifest.get('schema')!=FORMAT or manifest.get('dataSchema')!=SCHEMA_VERSION:raise ValueError('backup-version-unsupported')
        counts=manifest.get('counts')
        if (not isinstance(counts,dict) or set(counts)!={'accounts','messages','labelCacheRows','portraitCheckpoints'} or
            any(type(value) is not int or value<0 or value>9007199254740991 for value in counts.values())):
            raise ValueError('backup-counts-invalid')
        if not isinstance(manifest.get('createdUtc'),str) or len(manifest['createdUtc'])>64:raise ValueError('backup-manifest-invalid')
        if not isinstance(manifest.get('files'),list) or not manifest['files']:raise ValueError('backup-manifest-invalid')
        expected={'manifest.json'}
        for row in manifest['files']:
            if (not isinstance(row,dict) or set(row)!={'path','bytes','sha256'} or not isinstance(row['path'],str) or
                    not allowed_file(row['path']) or type(row['bytes']) is not int or row['bytes']<0 or
                    not isinstance(row['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',row['sha256'])):
                raise ValueError('backup-manifest-invalid')
            name='data/'+row['path']
            if name in expected:raise ValueError('backup-members-invalid')
            expected.add(name)
            entry=archive.getinfo(name)
            if entry.is_dir() or entry.file_size!=row['bytes'] or ((entry.external_attr>>16)&0o170000)==0o120000:
                raise ValueError('backup-members-invalid')
            with archive.open(entry) as stream:
                hasher=hashlib.sha256();count=0
                target=None
                try:
                    if extract_to is not None:
                        target_path=Path(extract_to)/row['path'];target_path.parent.mkdir(parents=True,exist_ok=True)
                        target=target_path.open('xb')
                    for chunk in iter(lambda:stream.read(1024*1024),b''):
                        count+=len(chunk)
                        if count>row['bytes']:raise ValueError('backup-size-mismatch')
                        hasher.update(chunk)
                        if target:target.write(chunk)
                finally:
                    if target:target.close()
            if count!=row['bytes'] or hasher.hexdigest()!=row['sha256']:raise ValueError('backup-content-mismatch')
        if {row.filename for row in entries}!=expected:raise ValueError('backup-members-invalid')
        checked_preferences(manifest.get('uiPreferences',{}))
        return manifest


def restore_backup(root,archive,expected_sha,*,client_root=None,fail_after=None):
    """Caller must have stopped the app and its owned bridge, verified by CLI helper."""
    root=checked_root(root);root.mkdir(parents=True,exist_ok=True)
    archive=Path(archive).absolute()
    if digest(archive)!=expected_sha:raise ValueError('backup-changed-after-preview')
    work=root/('.restore-'+uuid.uuid4().hex);stage=work/'prepared';rollback=work/'rollback'
    work.mkdir();stage.mkdir();rollback.mkdir()
    manifest=inspect_backup(archive,extract_to=stage)
    for row in manifest['files']:_validate_file(stage/row['path'],row['path'])
    same_user=manifest.get('windowsUserHash')==windows_user()
    runtime=stage/'real-client-runtime';runtime.mkdir(exist_ok=True)
    config=runtime/'qq-connection.json'
    if config.is_file():
        value=json.loads(config.read_text(encoding='utf-8'));value['enabled']=False
        if not same_user:value['bearer']=None
        config.write_text(json.dumps(value),encoding='utf-8')
    for name in ('model-source.json','api-model-source.json'):
        api=runtime/name
        if api.is_file():
            value=json.loads(api.read_text(encoding='utf-8'))
            value['selectedMode']='local'
            if not same_user and isinstance(value.get('api'),dict):value['api']['encryptedKey']=None
            api.write_text(json.dumps(value),encoding='utf-8')
    model=runtime/'local-model-source.json'
    if client_root and model.is_file():
        value=json.loads(model.read_text(encoding='utf-8'))
        value['path']=str(Path(client_root).resolve()/'.models/laya')
        model.write_text(json.dumps(value),encoding='utf-8')
    rebase_account_registry(stage,root)
    counts=_counts(stage)
    if counts!=manifest.get('counts'):raise ValueError('backup-counts-mismatch')
    import psutil
    state={'schema':FORMAT,'state':'preparing','operation':work.name,'counts':counts,
        'pid':os.getpid(),'pidCreateTime':psutil.Process().create_time(),
        'originalExists':{folder:(root/folder).exists() for folder in ('accounts','real-client-data','real-client-runtime')},
        'createdUtc':manifest['createdUtc'],'credentialsRequireReentry':not same_user,
        'uiPreferences':manifest.get('uiPreferences',{}),'connectionRequiresConfirmation':True,
        'restoredData':[],'movedOriginals':[]}
    journal=work/'journal.json'
    def save():
        temporary=journal.with_suffix('.tmp');temporary.write_text(json.dumps(state),encoding='utf-8');os.replace(temporary,journal)
    save()
    try:
        for index,folder in enumerate(('accounts','real-client-data','real-client-runtime')):
            source=root/folder;prepared=stage/folder
            if not source.resolve().is_relative_to(root.resolve()) or not (rollback/folder).resolve().is_relative_to(work.resolve()):
                raise ValueError('unsafe-restore-target')
            if source.exists():
                if _reparse(source):raise ValueError('unsafe-restore-target')
                os.replace(source,rollback/folder);state['movedOriginals'].append(folder);save()
            prepared.mkdir(exist_ok=True)
            os.replace(prepared,source);state['restoredData'].append(folder);save()
            if fail_after==index+1:raise OSError('synthetic-restore-interruption')
        state['state']='complete';save()
        (root/'real-client-runtime/restore-status.json').write_text(json.dumps(state),encoding='utf-8')
        return {**state,'rollbackDirectory':str(rollback)}
    except BaseException:
        # New files and all old files stay recoverable; never recursively delete.
        for folder in reversed(state['restoredData']):
            if (root/folder).exists():os.replace(root/folder,stage/folder)
        for folder in reversed(state['movedOriginals']):os.replace(rollback/folder,root/folder)
        state['state']='rolled-back';save()
        raise


def recover_pending_restores(root,*,process_alive=None):
    """Recover interrupted directory swaps before the next bridge opens any DB."""
    root=checked_root(root)
    for work in root.glob('.restore-*'):
        if _reparse(work) or not work.is_dir():raise ValueError('unsafe-restore-work')
        journal=work/'journal.json'
        if not journal.is_file():continue
        state=json.loads(journal.read_text(encoding='utf-8'))
        if state.get('schema')!=FORMAT or state.get('state')!='preparing':continue
        if process_alive is not None and process_alive(state):raise RuntimeError('data-restore-in-progress')
        rollback=work/'rollback';recovered=work/'recovered-new';recovered.mkdir(exist_ok=True)
        _check_root(rollback);_check_root(recovered)
        for folder in reversed(('accounts','real-client-data','real-client-runtime')):
            original=rollback/folder;current=root/folder
            if _reparse(original) or _reparse(current) or not original.resolve().is_relative_to(root.resolve()) or not current.resolve().is_relative_to(root.resolve()):
                raise ValueError('unsafe-recovery-target')
            if original.exists():
                if current.exists():os.replace(current,recovered/folder)
                os.replace(original,current)
            elif not state.get('originalExists',{}).get(folder,False) and current.exists() and not (work/'prepared'/folder).exists():
                os.replace(current,recovered/folder)
        state['state']='recovered-rollback'
        journal.write_text(json.dumps(state),encoding='utf-8')

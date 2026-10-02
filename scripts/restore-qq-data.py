"""Native handoff helper: wait for this installation, restore, optionally reopen."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
import psutil


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--client-root',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--wait-pid',type=int,default=0)
    parser.add_argument('--wait-create-time',type=float,default=0)
    parser.add_argument('--restart',action='store_true')
    args=parser.parse_args()
    client=args.client_root.resolve(strict=True)
    if not re.fullmatch('[0-9a-f]{64}',args.sha256):raise ValueError('invalid-backup-hash')
    sys.path.insert(0,str(client/'bridge'))
    from qq_data_management import checked_root,restore_backup
    data=checked_root(client/'QQVibeData')
    deadline=time.monotonic()+120
    def previous_app_alive():
        if not args.wait_pid:return False
        try:return abs(psutil.Process(args.wait_pid).create_time()-args.wait_create_time)<0.01
        except psutil.NoSuchProcess:return False
    def owned_workers_alive():
        prefix=str(client).casefold()
        for process in psutil.process_iter(['pid','name','cmdline']):
            try:
                if process.pid!=os.getpid() and process.info['name'].casefold() in ('python.exe','node.exe') and any(
                        prefix in str(arg).casefold() for arg in process.info['cmdline'] or []):return True
            except (psutil.AccessDenied,psutil.NoSuchProcess):continue
        return False
    while previous_app_alive() or owned_workers_alive():
        if time.monotonic()>deadline:raise RuntimeError('owned-app-not-stopped')
        time.sleep(0.25)
    def reopen():
        executable=client.parent.parent/'QQVibe.exe'
        if not executable.is_file():raise ValueError('packaged-app-missing')
        environment=os.environ.copy()
        for name in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','NODE_PATH','NODE_OPTIONS','LAYA_MODEL_DIR'):
            environment.pop(name,None)
        subprocess.Popen([str(executable)],cwd=executable.parent,env=environment,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:result=restore_backup(data,args.archive,args.sha256,client_root=client)
    except Exception as error:
        if args.restart:
            runtime=data/'real-client-runtime';runtime.mkdir(parents=True,exist_ok=True)
            (runtime/'restore-status.json').write_text(json.dumps({'state':'failed','uiPreferences':{},
                'reason':type(error).__name__,'windowsError':getattr(error,'winerror',None)}),encoding='utf-8')
            reopen()
        raise
    print(json.dumps({'state':result['state'],'counts':result['counts'],
        'credentialsRequireReentry':result['credentialsRequireReentry']}))
    if args.restart:reopen()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'state':'failed','reason':str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)

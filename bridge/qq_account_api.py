"""QQ account lifecycle using only the QQ product's derived stores."""
from __future__ import annotations

from account_api import AccountAPI
from backend_contracts import AccountUnavailableError, MessagesUnavailableError
from account_store import AccountConflict
from qq_account_store import QQAccountStore
from contextlib import contextmanager,ExitStack


class QQAccountAPI(AccountAPI):
    def __init__(self, backend, data_root, *, sync_manager=None):
        store = QQAccountStore(data_root)
        super().__init__(backend, store.data_dir, store=store,
                         forget_live=lambda _: True, forget_inactive=lambda _: True)
        self.sync_manager = sync_manager
        from qq_import import QQImport
        self.imports = QQImport(self)

    @contextmanager
    def data_snapshot_locks(self):
        # Same order as account/selection operations and published DTO reads.
        with self.lock,self.backend.selection_store.lock,self.backend.source.lock,ExitStack() as stack:
            try:
                account=self.backend.source.identity()[0]
                stack.enter_context(self.backend.source.read_library(account))
            except AccountUnavailableError:pass
            for store in sorted(self.backend.stores.values(),key=lambda value:str(value.path)):
                stack.enter_context(store.profile_lock)
            yield

    def backup_data(self,destination,ui_preferences=None):
        from qq_data_management import create_backup
        return create_backup(self.store.root,destination,lock_scope=self.data_snapshot_locks,ui_preferences=ui_preferences)

    def preview_restore(self,path):
        from qq_data_management import inspect_backup,digest,windows_user,_validate_file,_counts
        import tempfile
        from pathlib import Path
        location=self.store.root/'backups';location.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.restore-preview-',dir=location) as temporary:
            prepared=Path(temporary).resolve()
            if not prepared.is_relative_to(location.resolve()):raise ValueError('unsafe-backup-preview')
            manifest=inspect_backup(path,extract_to=prepared)
            for row in manifest['files']:_validate_file(prepared/row['path'],row['path'])
            if _counts(prepared)!=manifest['counts']:raise ValueError('backup-counts-mismatch')
        return {'schema':manifest['schema'],'createdUtc':manifest['createdUtc'],'counts':manifest['counts'],
            'sha256':digest(path),'credentialsRequireReentry':manifest.get('windowsUserHash')!=windows_user(),
            'connectionRequiresConfirmation':True}

    def restore_status(self):
        import json
        from account_store import _regular
        path=self.runtime_dir/'restore-status.json'
        if not _regular(path):return {'state':'none'}
        if path.stat().st_size>65536:raise ValueError('restore-status-invalid')
        value=json.loads(path.read_text(encoding='utf-8'))
        return {name:value.get(name) for name in ('state','operation','counts','createdUtc','uiPreferences','reason','windowsError',
            'credentialsRequireReentry','connectionRequiresConfirmation')}

    def set_conversation_reading(self,account,user,enabled):
        from backend_contracts import AccountChangedError
        if self.backend.source.identity()[0]!=account:raise AccountChangedError()
        self.backend.source.conversation_kind(user)
        result=self.backend.selection_store.set_reading(account,user,enabled)
        if self.sync_manager is not None:self.sync_manager.reading_changed(account,user,enabled)
        return result

    def clear_conversation(self,account,user):
        import uuid
        from backend_contracts import AccountChangedError
        if self.backend.source.identity()[0]!=account:raise AccountChangedError()
        self.backend.source.conversation_kind(user)
        self.set_conversation_reading(account,user,False)
        backup=self.store.root/'backups'/('before-conversation-clear-'+uuid.uuid4().hex+'.zip')
        recovery=self.backup_data(backup)
        selection=self.backend.selection_store
        with self.lock,selection.lock,self.backend.source.read_library(account) as (_,library):
            _account,_workdir,results=self.backend._scoped_identity()
            with results.profile_lock:
                import sqlite3
                from contextlib import closing
                from account_store import _regular,_check_root
                scoped_backups=[]
                def scope_rows(connection):
                    saved=[]
                    for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                        quoted='"'+table.replace('"','""')+'"'
                        columns=[row[1] for row in connection.execute('PRAGMA table_info('+quoted+')')]
                        if {'account','session'}<=set(columns):
                            rows=connection.execute('SELECT rowid,* FROM '+quoted+' WHERE account=? AND session=?',(account,user)).fetchall()
                            if rows:saved.append((quoted,['rowid',*columns],rows))
                    return saved
                def restore_scoped_rows():
                    for path,saved in scoped_backups:
                        with closing(sqlite3.connect(path)) as recovery,recovery:
                            recovery.execute('PRAGMA defer_foreign_keys=ON')
                            for quoted,columns,rows in saved:
                                recovery.execute('DELETE FROM '+quoted+' WHERE account=? AND session=?',(account,user))
                                names=','.join('"'+name.replace('"','""')+'"' for name in columns)
                                recovery.executemany('INSERT INTO '+quoted+' ('+names+') VALUES ('+','.join('?' for _ in columns)+')',rows)
                stage_files=[]
                for folder in ('analysis-staging','api-analysis-staging'):
                    directory=library.path.parent/folder
                    if directory.exists():
                        _check_root(directory)
                        stage_files.extend(directory.rglob('*.sqlite3'))
                try:
                    for path in stage_files:
                        _check_root(path.parent);_regular(path)
                        with closing(sqlite3.connect(path)) as stage,stage:
                            saved=scope_rows(stage)
                            scoped_backups.append((path,saved))
                            stage.execute('PRAGMA defer_foreign_keys=ON')
                            for quoted,_columns,_rows in saved:
                                stage.execute('DELETE FROM '+quoted+' WHERE account=? AND session=?',(account,user))
                except BaseException:
                    restore_scoped_rows();raise
                conn=library.connection
                conn.execute('ATTACH DATABASE ? AS qq_results',(str(results.path),))
                try:
                    if (conn.execute('PRAGMA main.journal_mode').fetchone()[0] not in ('delete','persist','truncate') or
                        conn.execute('PRAGMA qq_results.journal_mode').fetchone()[0] not in ('delete','persist','truncate')):
                        raise ValueError('atomic-conversation-clear-unavailable')
                    with library.transaction() as cursor:
                        cursor.execute('PRAGMA defer_foreign_keys=ON')
                        cursor.execute('DELETE FROM message_observations WHERE message_key IN '
                            '(SELECT message_key FROM messages WHERE account_key=? AND conversation_key=?)',(account,user))
                        for schema,owner,session in [('qq_results','account','session'),('main','account_key','conversation_key')]:
                            tables=[row[0] for row in cursor.execute('SELECT name FROM '+schema+".sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                            for table in sorted(tables,key=lambda value:value=='conversations'):
                                quoted='"'+table.replace('"','""')+'"'
                                columns={row[1] for row in cursor.execute('PRAGMA '+schema+'.table_info('+quoted+')')}
                                if {owner,session}<=columns:
                                    cursor.execute('DELETE FROM '+schema+'.'+quoted+' WHERE '+owner+'=? AND '+session+'=?',(account,user))
                except BaseException:
                    restore_scoped_rows();raise
                finally:conn.execute('DETACH DATABASE qq_results')
                # Revoking every result generation first makes late writes fail,
                # even if a later explicit add creates the same conversation again.
            if self.sync_manager is not None:self.sync_manager.selection_changed(account,user,False)
        return {'account':account,'user':user,'state':'cleared','recoveryBackup':recovery['path'],
            'originalQQModified':False}

    def _live_account(self, deleting=False):
        try:
            return self.backend.source.identity()[0]
        except AccountUnavailableError:
            return None

    def source_status(self):
        """Public status of this offline adapter, independent of model readiness.

        Attaching a local account is not proof of a QCE connection. The future
        coordinator must supply its own state when the live connector is wired.
        """
        ready = False
        try:
            self.backend.source.verified_identity(messages=True)
            ready = True
        except (AccountUnavailableError, MessagesUnavailableError):
            pass
        sync = (self.sync_manager.public() if self.sync_manager is not None else
                {"enabled": False, "state": "unavailable", "reason": "connector-pending"})
        return {"platform": "qq", "connection": "online" if sync.get("state") in ("online", "fetching") else "offline",
                "localReady": ready, "sync": sync}

    def activate(self, identifier):
        """Choose an already-registered offline library; never log in or create data."""
        if self.sync_manager is not None:
            self.sync_manager.pause_binding()
        try:
            with self.lock, self.backend.source.lock:
                if self.deleting:
                    raise AccountConflict("账号清理正在进行")
                item = self.store.resolve(identifier)
                if item.get("deletionId"):
                    raise AccountConflict("此账号清理尚未完成")
                if not (self.store._workdir(item["account"]) / "messages.sqlite").is_file():
                    raise AccountConflict("此账号的本地消息库不可用")
                account = self.backend.source.attach(item["ownerUin"])
                return {"account": account, "accountId": identifier, "messagesReady": True, "offline": True}
        finally:
            if self.sync_manager is not None:
                self.sync_manager.resume_binding()

    def delete(self, identifier):
        item = self.store.resolve(identifier)
        account = item["account"]
        self.imports.stop_account(account)
        was_current = self._live_account() == account
        if self.sync_manager is not None:
            # This hook must drain and persist the account's disabled sync state.
            self.sync_manager.stop_account(account)
        try:
            return super().delete(identifier)
        except BaseException:
            if was_current:
                if self.store.is_deleting(account):
                    self._mark_no_recovery()
                elif (self.store._workdir(account) / "messages.sqlite").is_file():
                    self.backend.source.attach(item["ownerUin"])
            raise

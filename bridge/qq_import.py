"""One cancellable, disk-backed QQ export preview and atomic local commit."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

from account_store import AccountConflict, AccountNotFound, account_id
from qq_identity import account_key, canonical_uin
from qq_import_reader import stream_export
from qq_json_reader import stream_json
import qq_json_adapter as json_adapter
from qq_message_store import QQMessageStore, StoreError
from qq_normalize import (COUNTER_FIELDS, REASON_COUNTERS, ExportFormatError,
                          NormalizationError, normalize_export, normalize_export_row, normalize_message)


class ImportCancelled(Exception):
    pass


class QQImport:
    def __init__(self, accounts, *, max_rows=2000000, max_bytes=2 * 1024 * 1024 * 1024):
        self.accounts = accounts
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self.lock = threading.RLock()
        self.lifecycle = threading.Lock()
        self.job = None
        self.closed = False

    def _check(self, job):
        if job["cancel"].is_set() or self.closed:
            raise ImportCancelled()

    def _update(self, job, **values):
        with self.lock:
            job["public"].update(values)

    def status(self, identifier):
        with self.lock:
            job = self._job(identifier)
            return json.loads(json.dumps(job["public"], ensure_ascii=False))

    def _job(self, identifier):
        if not self.job or identifier != self.job["public"]["jobId"]:
            raise ValueError("unknown-import-job")
        return self.job

    def _cleanup(self, job):
        temporary = job.pop("temporary", None)
        if temporary is not None:
            temporary.cleanup()

    def _run(self, job, action):
        try:
            self._check(job)
            action(job)
        except ImportCancelled:
            self._update(job, state="cancelled", error=None)
        except UnicodeDecodeError:
            self._update(job, state="failed", error="invalid-export-encoding")
        except (ExportFormatError, NormalizationError) as exc:
            self._update(job, state="failed", error=str(exc))
        except StoreError as exc:
            self._update(job, state="failed", error="peer-identity-mismatch" if str(exc) == "peer-identity-mismatch" else "import-unavailable")
        except AccountConflict:
            self._update(job, state="failed", error="unsafe-export-path")
        except PermissionError:
            self._update(job, state="failed", error="export-permission-denied")
        except OSError:
            self._update(job, state="failed", error="export-io-error")
        except Exception:
            # Paths, SQL, source bodies and tracebacks never enter the status DTO.
            self._update(job, state="failed", error="import-unavailable")
        finally:
            if job["public"]["state"] in ("cancelled", "failed", "complete"):
                self._cleanup(job)

    def _launch(self, job, action, state):
        self._update(job, state=state, error=None)
        worker = threading.Thread(target=self._run, args=(job, action), daemon=True,
                                  name="qq-file-import")
        job["thread"] = worker
        worker.start()

    def _cancel(self, job):
        job["cancel"].set()
        worker = job.get("thread")
        if worker and worker is not threading.current_thread():
            worker.join(timeout=30)
            if worker.is_alive():
                raise AccountConflict("文件导入尚未停止，请稍后重试")
        if job["public"]["state"] not in ("complete", "failed"):
            self._update(job, state="cancelled")
        self._cleanup(job)

    def start(self, path, owner=None, mapping=None):
        if not isinstance(path, str) or not path or len(path) > 32768 or "\0" in path:
            raise ValueError("invalid-export-path")
        if owner is not None:
            owner = canonical_uin(owner)
        mapping = json_adapter.validate_options(mapping)
        path = path.strip()
        if len(path) > 1 and path[0] == path[-1] == '"':
            path = path[1:-1]
        with self.lifecycle:
            if self.closed:
                raise ValueError("import-closing")
            if self.job:
                self._cancel(self.job)
            temporary = tempfile.TemporaryDirectory(prefix="qq-import-")
            job = {"public": {"jobId": uuid.uuid4().hex, "state": "reading", "rowsRead": 0,
                              "fileName": Path(path).name[:256], "error": None},
                   "path": path, "owner": owner, "mapping": mapping, "cancel": threading.Event(),
                   "temporary": temporary, "database": Path(temporary.name) / "preview.sqlite"}
            with self.lock:
                self.job = job
            self._launch(job, self._read, "reading")
            return self.status(job["public"]["jobId"])

    def _read(self, job):
        participants, count = {}, 0
        snapshot_digest = hashlib.sha256()
        with closing(sqlite3.connect(job["database"])) as db, db:
            db.execute("CREATE TABLE raw_rows(idx INTEGER PRIMARY KEY, raw TEXT, reason TEXT)")
            db.execute("CREATE TABLE records(idx INTEGER PRIMARY KEY, record TEXT, native_id TEXT)")
            def row(value, reason):
                nonlocal count
                self._check(job)
                count += 1
                snapshot_digest.update(json.dumps({"row": value, "reason": reason}, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
                if count > self.max_rows:
                    raise ExportFormatError("row-budget-exceeded")
                db.execute("INSERT INTO raw_rows VALUES (?,?,?)",
                           (count, json.dumps(value, ensure_ascii=False), reason))
                if count % 250 == 0:
                    self._update(job, rowsRead=count)
            try:
                if job['mapping'].get('recordPath') or Path(job['path']).suffix.lower() in ('.jsonl', '.ndjson'):
                    raise ExportFormatError('unsupported-export-format')
                document, format_kind = stream_export(job["path"], row,
                    cancel=lambda: self._check(job), max_bytes=self.max_bytes)
                if not isinstance(document.get('metadata'), dict) or not isinstance(document.get('chatInfo'), dict):
                    raise ExportFormatError('unsupported-export-format')
            except ExportFormatError as exc:
                if str(exc) not in ('unsupported-export-format', 'invalid-export-json', 'export-item-too-large'):
                    raise
                db.execute('DELETE FROM raw_rows')
                count, snapshot_digest = 0, hashlib.sha256()
                self._update(job, rowsRead=0)
                document, format_kind = stream_json(job['path'], row,
                    cancel=lambda: self._check(job), max_bytes=self.max_bytes,
                    record_path=job['mapping'].get('recordPath', ''))
        # Fingerprint the parsed input snapshot, not the original file bytes or
        # its path. Explicit identity mapping remains separately account-scoped.
        snapshot_digest.update(json.dumps({"header": document, "format": format_kind}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8"))
        job["sourceSnapshot"] = snapshot_digest.hexdigest()
        metadata = document.get("metadata")
        declared_version = metadata.get("version") if isinstance(metadata, dict) else None
        job["sourceVersion"] = declared_version if isinstance(declared_version, str) and len(declared_version) <= 128 and re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", declared_version) else None
        job['adapted'] = not json_adapter.native_document(document, job['mapping'])
        # This table retains exactly the parsed source snapshot. Rejected rows
        # remain counted; field conversion never mutates the source file.
        known_fields, unmapped, samples, rejected = set(), set(), [], {}
        scopes, generated_ids = set(), 0
        native = not job['adapted']
        info = document.get('chatInfo') if native else json_adapter.header(document, job['mapping'])
        if not isinstance(info, dict):
            raise ExportFormatError('invalid-export-metadata')
        qce = isinstance(metadata, dict) and str(metadata.get('name', '')).startswith('QQChatExporter')
        selected = document.get('_selectedArrays', ['/messages'])
        if len(selected) != 1:
            self._update(job, state='mapping', rowsRead=0,
                reason='multiple-message-arrays' if len(selected) > 1 else 'message-array-required',
                schema={'arrayPaths': document.get('_arrayPaths', []), 'fields': [], 'senders': [], 'samples': []})
            return
        with closing(sqlite3.connect(job['database'])) as db, db:
            db.execute('CREATE TABLE adapted_rows(idx INTEGER PRIMARY KEY, raw TEXT, reason TEXT, id_kind TEXT)')
            for index, raw, reason in db.execute('SELECT idx,raw,reason FROM raw_rows ORDER BY idx'):
                self._check(job)
                value = json.loads(raw)
                if len(known_fields) < 256:
                    known_fields.update(json_adapter.fields(value))
                id_kind = 'qce-msgId'
                if not native:
                    try:
                        scope = json_adapter.row_scope(value)
                        if scope:
                            scopes.add(scope)
                        if len(scopes) > 1:
                            raise ExportFormatError('multiple-conversations')
                        if isinstance(value, dict):
                            sender = json_adapter.field(value, 'sender', job['mapping'])
                            try:
                                canonical_uin(job['mapping'].get('senders', {}).get(str(sender), sender))
                            except ValueError:
                                if sender is not None and len(unmapped) < 200:
                                    unmapped.add(str(sender)[:256])
                            if len(samples) < 3:
                                samples.append({'sender': str(sender or '')[:128],
                                    'time': str(json_adapter.field(value, 'time', job['mapping'], qce=qce) or '')[:128],
                                    'text': (json_adapter.text_content(json_adapter.field(value, 'text', job['mapping'])) or '[非文本消息]')[:200]})
                        if not reason:
                            value, id_kind = json_adapter.adapt(value, job['mapping'],
                                snapshot=job['sourceSnapshot'], index=index, qce=qce,
                                chatlab=isinstance(document.get('chatlab'), dict))
                            generated_ids += id_kind == 'json-file-row'
                    except ExportFormatError:
                        raise
                    except NormalizationError as exc:
                        reason = exc.reason
                    except ValueError:
                        raise ExportFormatError('invalid-conversation-identity') from None
                if reason:
                    rejected[reason] = rejected.get(reason, 0) + 1
                db.execute('INSERT INTO adapted_rows VALUES (?,?,?,?)',
                    (index, json.dumps(value, ensure_ascii=False), reason, id_kind))
                # Rejected rows can still supply native sender evidence. Never
                # use a third sender to silently narrow a multi-chat to one peer.
                if isinstance(value, dict) and value.get('system', value.get('isSystemMessage', False)) is not True:
                    sender = value.get('sender')
                    if isinstance(sender, dict):
                        try:
                            uin = canonical_uin(sender.get('uin'))
                        except ValueError:
                            continue
                        entry = participants.setdefault(uin, {'uin': uin, 'name': str(sender.get('name') or '')[:128],
                            'nickname': str(sender.get('nickname') or sender.get('name') or '')[:256],
                            'cardName': str(sender.get('groupCard') or sender.get('cardName') or '')[:256], 'uids': set()})
                        uid = sender.get('uid')
                        if isinstance(uid, str) and uid:
                            entry['uids'].add(uid)
                        if len(participants) > 10000:
                            raise ExportFormatError('participant-budget-exceeded')
                        if len(entry['uids']) > 1:
                            raise ExportFormatError('peer-identity-mismatch')
        if scopes:
            scope_kind, peer = next(iter(scopes))
            declared_kind = 'friend' if info.get('type') in ('friend', 'private') else info.get('type')
            if declared_kind and declared_kind != scope_kind:
                raise ExportFormatError('conversation-kind-mismatch')
            if info.get('peerUid') and str(info['peerUid']) != peer:
                raise ExportFormatError('peer-identity-mismatch')
            info.update(type=scope_kind, peerUid=peer)
        schema = {'arrayPaths': document.get('_arrayPaths', ['/messages']), 'fields': sorted(known_fields)[:256],
                  'senders': sorted(unmapped), 'samples': samples, 'rejections': rejected,
                  'generatedIds': generated_ids}
        self._update(job, schema=schema)
        if not native and not info.get('type') and len(participants) > 2:
            self._update(job, state='mapping', reason='conversation-kind-required', rowsRead=count)
            return
        document['chatInfo'] = info
        if not isinstance(document.get('metadata'), dict):
            document['metadata'] = {}
        job["document"], job["participants"] = document, participants
        job['generatedIds'] = generated_ids
        if job['adapted']:
            format_kind = 'generic-jsonl' if format_kind.endswith('jsonl') else 'generic-json'
        self._update(job, rowsRead=count, format=format_kind,
                     participants=[{"uin": item["uin"], "name": item["name"]} for item in participants.values()])
        self._normalize(job)

    def _normalize(self, job):
        # The existing whole-container identity rules see one representative per
        # participant. No synthetic message is written or included in row counts.
        header = {"metadata": job["document"].get("metadata"), "chatInfo": job["document"].get("chatInfo"),
                  "messages": [{"sender": {"uin": item["uin"], "uid": next(iter(item["uids"]), None)}}
                               for item in job["participants"].values()]}
        info=job['document'].get('chatInfo') or {}
        kind='group' if info.get('type')=='group' else 'friend'
        if kind=='friend' and len(job['participants'])>2:
            raise ExportFormatError('not-single-chat')
        identity = normalize_export(header, self_uin=job["owner"],expected_kind=kind,
                                    adapted=job.get('adapted', False))
        if identity["status"] == "pending-identity":
            declared = job["owner"] or job["document"].get("chatInfo", {}).get("selfUin")
            self._update(job, state="identity", reason=identity["reason"], ownerUin=declared)
            return
        owner = identity["ownerUin"]
        self._update(job, ownerUin=owner)
        declared_count = job["document"].get("metadata", {}).get("messageCount")
        if declared_count is not None and (type(declared_count) is not int or declared_count != job["public"]["rowsRead"]):
            raise ExportFormatError("export-count-mismatch")
        counts = {name: 0 for name in COUNTER_FIELDS}
        rejects, samples, times, directions = [], [], [], {"self": 0, "peer": 0, "system": 0, "conflict": 0}
        with closing(sqlite3.connect(job["database"])) as db, db:
            db.execute("DELETE FROM records")
            for index, raw, reason, id_kind, original in db.execute("SELECT a.idx,a.raw,a.reason,a.id_kind,r.raw FROM adapted_rows a JOIN raw_rows r ON r.idx=a.idx ORDER BY a.idx"):
                self._check(job)
                counts["rowsTotal"] += 1
                try:
                    if reason:
                        raise NormalizationError(reason)
                    source = json.loads(original)
                    if job.get('adapted') and id_kind == 'qce-msgId' and isinstance(source, dict) and 'msgId' in source and 'msgTime' in source:
                        record, flags = normalize_message(source, self_uin=owner,
                            expected_kind=kind, self_uid=info.get('selfUid'))
                        if record['conversation_key'] != identity['conversationKey']:
                            raise NormalizationError('pending-identity')
                    else:
                        record, flags = normalize_export_row(json.loads(raw), owner,
                            identity.get('groupCode') if kind=='group' else identity['peerUid'],
                            expected_kind=kind,self_uid=info.get('selfUid'))
                    record['native_id_kind'] = id_kind
                    if job.get('adapted'):
                        record['raw'] = original
                except NormalizationError as exc:
                    counts["rowsRejected"] += 1
                    counts[REASON_COUNTERS.get(exc.reason, "invalidId")] += 1
                    if len(rejects) < 20:
                        rejects.append({"row": index, "reason": exc.reason})
                    continue
                counts["unknownTypes"] += int(flags["unknownType"])
                counts["unknownRecall"] += int(flags["unknownRecall"])
                counts["directionConflicts"] += int(flags["directionConflict"])
                counts["rowsConflict" if record["status"] == "conflict" or flags["directionConflict"] else "rowsOk"] += 1
                db.execute("INSERT INTO records VALUES (?,?,?)", (index, json.dumps(record, ensure_ascii=False), id_kind + ':' + record["native_id"]))
                directions[record["direction"]] += 1
                timestamp = record["time_ms"]
                times = [min(times[0], timestamp), max(times[1], timestamp)] if times else [timestamp, timestamp]
                if len(samples) < 3:
                    samples.append({"side": record["direction"], "text": (record["text"] or "[非文本消息]")[:200], "time": timestamp})
            unique = db.execute("SELECT COUNT(DISTINCT native_id) FROM records").fetchone()[0]
        info = job["document"]["chatInfo"]
        job["identity"] = {key: identity[key] for key in ("ownerUin", "peerUin", "peerUid", "conversationKey")}
        if kind=='group':
            job['identity'].update(kind='group',groupCode=identity['groupCode'])
            from qq_entities import UID
            own_uid=info.get('selfUid')
            if isinstance(own_uid,str) and UID.fullmatch(own_uid):
                job['identity'].update(ownerUid=own_uid,ownerName=str(info.get('selfName') or '')[:256])
        job["identity"]["name"] = str(info.get("name") or identity.get('groupCode') or identity["peerUin"] or identity["peerUid"])[:256]
        self._check(job)
        self._update(job, state="ready", reason=None, previewToken=uuid.uuid4().hex,
            preview={**job["identity"], "account": account_key(owner), "counts": counts,
                     "uniqueMessages": unique, "duplicatesInFile": counts["rowsOk"] + counts["rowsConflict"] - unique,
                     "range": times or None, "directions": directions, "rejected": rejects, "samples": samples})

    def map_owner(self, identifier, owner, peer_uid=None):
        owner = canonical_uin(owner)
        with self.lifecycle:
            job = self._job(identifier)
            if job["public"]["state"] not in ("identity", "ready"):
                raise ValueError("import-not-awaiting-identity")
            if peer_uid is not None:
                from qq_normalize import UID
                group=(job['document'].get('chatInfo') or {}).get('type')=='group'
                if not isinstance(peer_uid, str) or not (re.fullmatch(r'[1-9][0-9]{0,127}',peer_uid) if group else UID.fullmatch(peer_uid)):
                    raise ValueError("invalid-peer-uid")
                declared = job["document"]["chatInfo"].get("peerUid")
                if declared is not None and declared != peer_uid:
                    raise ValueError("peer-identity-mismatch")
                job["document"]["chatInfo"]["peerUid"] = peer_uid
            job["owner"] = owner
            self._launch(job, self._normalize, "normalizing")
            return self.status(identifier)

    def commit(self, identifier, token, accept_partial=False):
        with self.lifecycle:
            job = self._job(identifier)
            public = job["public"]
            if public["state"] != "ready" or token != public.get("previewToken"):
                raise ValueError("stale-import-preview")
            if type(accept_partial) is not bool:
                raise ValueError("invalid-partial-choice")
            if public["preview"]["counts"]["rowsRejected"] and not accept_partial:
                raise ValueError("partial-import-confirmation-required")
            if not public["preview"]["uniqueMessages"]:
                raise ValueError("no-importable-messages")
            self._launch(job, self._commit, "committing")
            return self.status(identifier)

    def _commit(self, job):
        info = job["identity"]
        account = account_key(info["ownerUin"])
        # Own connection: a long atomic import must not hold the source read lock.
        # QQAccountAPI.delete drains this worker before moving any account files.
        with self.accounts.lock:
            self._check(job)
            if self.accounts.deleting or self.accounts.backend.closing or self.accounts.store.is_deleting(account):
                raise ImportCancelled()
            directory = self.accounts.store._workdir(account)
            library = QQMessageStore(directory / "messages.sqlite")
            try:
                try:
                    nickname = self.accounts.store.resolve(account_id(account)).get("nickname", "")
                except AccountNotFound:
                    nickname = ""
                identifier = self.accounts.store.register(account, directory,
                    owner_uin=info["ownerUin"], nickname=nickname)
            except BaseException:
                library.close()
                raise
        try:
            with closing(sqlite3.connect(job["database"])) as staged:
                records = (json.loads(row[0]) for row in staged.execute("SELECT record FROM records ORDER BY idx"))
                outcome = library.ingest(account, info["conversationKey"], records,
                    conversation=info, cancel=lambda: self._check(job), now_ms=int(time.time() * 1000),
                    receipt={"kind": "file-import", "format": job["public"]["format"],
                        "status": "partial" if job["public"]["preview"]["counts"]["rowsRejected"] else "complete",
                        "reason": "normalization-rejected" if job["public"]["preview"]["counts"]["rowsRejected"] else None,
                        "rejectedRows": job["public"]["preview"]["counts"]["rowsRejected"],
                        "sourceSnapshot": job["sourceSnapshot"], "sourceVersion": job["sourceVersion"]},
                    receipt_clock=lambda: int(time.time() * 1000))
                if info.get('kind')=='group':
                    from qq_entities import qq_avatar
                    library.set_conversation_profile(account,info['conversationKey'],name=info['name'],avatar_url=qq_avatar(info['groupCode'],group=True),source_version=job['sourceVersion'])
                    library.set_members(account,info['conversationKey'],[{'uin':item['uin'],
                        'uid':next(iter(item['uids']),None),'nick':item['nickname'],'cardName':item['cardName']} for item in job['participants'].values()],source='file-observed')
        finally:
            library.close()
        self._update(job, state="complete", result={"account": account, "accountId": identifier,
            "conversationKey": info["conversationKey"], **outcome})

    def cancel(self, identifier):
        with self.lifecycle:
            job = self._job(identifier)
            self._cancel(job)
            return self.status(identifier)

    def stop_account(self, account):
        with self.lifecycle:
            job = self.job
            if not job:
                return
            owner = job.get("identity", {}).get("ownerUin") or job.get("owner")
            # Reading an unbound export must not survive account deletion either.
            if owner is None or account_key(owner) == account:
                self._cancel(job)

    def close(self):
        with self.lifecycle:
            self.closed = True
            if self.job:
                self._cancel(self.job)

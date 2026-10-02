"""Stream new messages through whole-message-batch inference and durable batch state."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import deque

from batch_state import BATCH_VERSION, BatchStateStore
from profile_signals import keyword_counts
from analysis_targets import SELF_SUBJECT, is_group, subject as target_subject, target


class BatchEngine:
    def __init__(self, backend):
        self.backend = backend
        self.stores = {}
        self.member_jobs = {}
        self.member_iterators = {}
        self.focused_member = None

    def subject(self, user, member=None):
        return target_subject(self.backend.source, user, member)

    def store(self, store):
        key = str(store.path)
        if key not in self.stores:
            self.stores[key] = BatchStateStore(store)
        return self.stores[key]

    def snapshot(self, account, user, version, store, member=None):
        return self.store(store).load(account, user, version, self.subject(user, member))

    def ensure(self, account, user, version, store, scope, member=None):
        subject = self.subject(user, member)
        batches = self.store(store)
        saved = batches.load(account, user, version, subject)
        if saved is not None:
            return saved
        state = self.backend._legacy_profile_state(account, user, version, store, scope[1], subject or None)
        previous = (store.progress(account, user, version) if member is None else
                    {'cursor':None,'context':[],'complete':False})
        cursor, context = previous["cursor"], previous["context"]
        # Old oversized messages were never inferred. The new window protocol can split them.
        skipped = store.first_skipped_position(account, user, version) if member is None else None
        if skipped and (cursor is None or tuple(skipped) <= tuple(cursor)):
            cursor = (skipped[0], skipped[1], max(0, skipped[2]-1))
            context = []
        self.backend._assert_scope(scope)
        saved = batches.seed(account, user, version, subject, state, cursor, list(context)[-3:])
        highwater = self.backend.source.history_highwater(user)
        if previous["complete"] and not skipped and (highwater is None or
                (cursor is not None and tuple(cursor) >= tuple(highwater))):
            saved = batches.mark_complete(account, user, version, subject)
        return saved

    def target(self, item, subject, *, is_group=False):
        # A single-chat conversation key need not equal the peer's native sender ID.
        # Member matching is meaningful only for an explicitly grouped conversation.
        if getattr(self.backend.source,'kind',None)!='qq' and item.get('side')=='self':
            return False
        return target(item, subject, is_group=is_group)

    @staticmethod
    def text_items(items):
        return [item for item in items if item["kind"] == "text" and item["text"].strip()]

    @staticmethod
    def context(items):
        from qq_roles import group_context
        return [{"id": item["id"], "side": item["side"], "text": item["text"],
                 **({'groupContext':group_context(item)} if item.get('conversationKind')=='group' else {})}
                for item in items if item.get("kind", "text") == "text"][-3:]

    def known(self, account, user, version, store, subject, items):
        ids = [item["id"] for item in items]
        if not ids:
            return set()
        batches = self.store(store)
        legacy = batches.legacy_known(account, user, version, subject, ids)
        return legacy | set(batches.outcomes(account, user, version, subject, ids=ids, limit=500))

    def outcomes(self, account, user, version, store, ids=None):
        rows = self.store(store).outcomes(account, user, version, self.subject(user), ids=ids,
                                          limit=500 if ids is not None else 80)
        result = {}
        for stable_id, row in rows.items():
            marker = {key: value for key, value in row.items() if key != "result"}
            marker["batch"] = {**marker.get("batch", {}), "id": marker["batchId"]}
            result[stable_id] = {**(row.get("result") or {}), **marker}
        return result

    def move(self, account, user, version, store, subject, cursor, context):
        """Advance only over already-covered or non-text rows; never invent model results."""
        if cursor is None:
            return
        self.store(store).move_cursor(account, user, version, subject, cursor, context)

    def infer(self, account, user, version, store, scope, subject, items, context, *, advance):
        batches = self.store(store)
        known = self.known(account, user, version, store, subject, items)
        offsets = batches.offsets(account, user, version, subject, [item["id"] for item in items])
        snapshot = batches.load(account, user, version, subject)
        if (advance and offsets.get(items[0]["id"], 0) > 0 and
                (tuple(items[0]["_sort"]) != snapshot["cursor"] or
                 offsets[items[0]["id"]] > snapshot["charOffset"])):
            # A recent-window pass may already have durably consumed more of this fragment.
            self.backend._assert_scope(scope)
            batches.move_cursor(account, user, version, subject, items[0]["_sort"], context,
                                char_offset=offsets[items[0]["id"]])
        payload = [{"id": item["id"], "text": item["text"], "side": item["side"],
                    **({key:item.get(key) for key in ('senderId','time','conversationKind','quote','mentions')} if item.get('conversationKind')=='group' else {}),
                    "target": self.target(item, subject, is_group=is_group(self.backend.source,user))
                              and item["id"] not in known,
                    "offset": offsets.get(item["id"], 0)} for item in items]
        started = time.perf_counter()
        response = self.backend.analyzer.analyze_batch(
            f"{account}:{store.path}:{user}:{subject}", payload,
            [{"side": item["side"], "text": item["text"],
              **({'groupContext':item['groupContext']} if item.get('groupContext') else {})} for item in context])
        inferred = time.perf_counter()
        records, words = [], {}
        for index, piece in enumerate(response["consumed"]):
            item = items[index]
            record = {"id": item["id"], "position": list(item["_sort"]),
                      "senderId": item["senderId"], "side": item["side"],
                      "target": payload[index]["target"],
                      "startOffset": piece["start"], "endOffset": piece["end"],
                      "textLength": len(item["text"]), "complete": piece["complete"]}
            records.append(record)
            if record["complete"] and record["target"]:
                words[item["id"]] = dict(keyword_counts([item["text"]]))
        last = records[-1]
        cursor, offset = tuple(last["position"]), 0 if last["complete"] else last["endOffset"]
        completed = [items[index] for index, record in enumerate(records) if record["complete"]]
        next_context = self.context([*context, *completed])
        snapshot = batches.load(account, user, version, subject)
        batch_id = hashlib.sha256(json.dumps([account, user, version, subject, BATCH_VERSION,
            [(item["id"], item["startOffset"], item["endOffset"], item["target"]) for item in records]],
            ensure_ascii=False).encode()).hexdigest()
        self.backend._assert_scope(scope)
        verified = time.perf_counter()
        batches.commit(account, user, version, subject, batch_id=batch_id, consumed=records,
            cursor=cursor if advance else snapshot["cursor"],
            char_offset=offset if advance else snapshot["charOffset"],
            context=next_context if advance else snapshot["context"], result=response["result"],
            word_counts=words, is_group=is_group(self.backend.source,user), advance=advance)
        finished = time.perf_counter()
        with self.backend.jobs_lock:
            metrics = self.backend.performance.setdefault((account, user, version), {})
            metrics["batchCalls"] = metrics.get("batchCalls", 0) + int(response["result"] is not None)
            metrics["batchTexts"] = metrics.get("batchTexts", 0) + sum(
                item["complete"] and item["id"] not in known for item in records)
            metrics["batchModelMs"] = metrics.get("batchModelMs", 0) + response.get("durationMs", 0)
            metrics["batchRequestMs"] = metrics.get("batchRequestMs", 0) + (inferred-started)*1000
            metrics["batchVerifyMs"] = metrics.get("batchVerifyMs", 0) + (verified-inferred)*1000
            metrics["batchSaveMs"] = metrics.get("batchSaveMs", 0) + (finished-verified)*1000
        return cursor, offset, next_context

    def recent(self, account, user, version, store, job, scope, limit, member=None):
        subject = self.subject(user, member)
        self.ensure(account, user, version, store, scope, member)
        window = self.backend.source.messages(user, limit+3)
        context = self.context(window[:-limit])
        items = self.text_items(window[-limit:])
        while items:
            known = self.known(account, user, version, store, subject, items)
            first = next((index for index, item in enumerate(items) if item["id"] not in known), None)
            if first is None:
                break
            context = self.context([*context, *items[:first]])
            items = items[first:]
            cursor, offset, context = self.infer(account, user, version, store, scope,
                                                subject, items, context, advance=False)
            job["processed"] = len(known)
            job["total"] = len(self.text_items(window[-limit:]))
            items = [item for item in items if tuple(item["_sort"]) > cursor or
                     (offset and tuple(item["_sort"]) == cursor)]
            yield
        job["processed"] = job["total"] = len(self.text_items(window[-limit:]))

    def incremental(self, account, user, version, store, job, scope, key, member=None):
        subject = self.subject(user, member)
        batches = self.store(store)
        saved = self.ensure(account, user, version, store, scope, member)
        cursor, offset, context = saved["cursor"], saved["charOffset"], saved["context"]
        job["phase"] = "incremental" if saved["complete"] else "baseline"
        job["checkpointComplete"] = False
        job["analysisUnit"] = "batch"
        job["processed"] = job["total"] = saved["state"]["count"]
        highwater = self.backend.source.history_highwater(user)
        buffered_page, buffered_end = [], None
        while highwater is not None and (cursor is None or cursor < highwater or offset):
            if member is None:
                recent = self.backend._take_recent_priority(key)
                if recent:
                    yield from self.recent(account, user, version, store, {}, scope, recent)
            if not buffered_page:
                seek = (cursor[0], cursor[1], cursor[2]-1) if offset else cursor
                self.backend._assert_scope(scope)
                buffered_page, buffered_end = self.backend.source.history_page(user, highwater, seek)
            page, next_cursor = buffered_page, buffered_end
            if next_cursor is None:
                break
            page_texts = self.text_items(page)
            if member is None:
                items = page_texts
                known = self.known(account, user, version, store, subject, items)
                first = next((index for index, item in enumerate(items) if item["id"] not in known), None)
                if first is None:
                    context = self.context([*context, *page_texts])
                    self.backend._assert_scope(scope)
                    self.move(account, user, version, store, subject, next_cursor, context)
                    cursor, offset = next_cursor, 0
                    buffered_page = []
                    continue
                context = self.context([*context, *items[:first]])
                if first:
                    self.backend._assert_scope(scope)
                    cursor, offset = tuple(items[first-1]["_sort"]), 0
                    self.move(account, user, version, store, subject, cursor, context)
                items = items[first:]
            else:
                # Only this member is TARGET; retain intervening speakers as ordered BACKGROUND.
                member_items = [item for item in page_texts if self.target(item, subject, is_group=is_group(self.backend.source,user))]
                known = self.known(account, user, version, store, subject, member_items)
                pending = [item for item in member_items if item["id"] not in known]
                if not pending:
                    context = self.context([*context, *page_texts])
                    self.backend._assert_scope(scope)
                    self.move(account, user, version, store, subject, next_cursor, context)
                    cursor, offset = next_cursor, 0
                    buffered_page = []
                    continue
                first_position = tuple(pending[0]["_sort"])
                last_position = tuple(pending[-1]["_sort"])
                # Resume a partial background span before the next member target, if any.
                start_position = cursor if offset and cursor is not None and cursor <= first_position else first_position
                before = [item for item in page_texts if
                          (cursor is None or tuple(item["_sort"]) > cursor) and
                          tuple(item["_sort"]) < start_position]
                context = self.context([*context, *before])
                if before:
                    self.backend._assert_scope(scope)
                    cursor, offset = tuple(before[-1]["_sort"]), 0
                    self.move(account, user, version, store, subject, cursor, context)
                items = [item for item in page_texts if
                         start_position <= tuple(item["_sort"]) <= last_position]
            cursor, offset, context = self.infer(account, user, version, store, scope,
                                                subject, items, context, advance=True)
            state = batches.load(account, user, version, subject)["state"]
            job["processed"] = job["total"] = state["count"]
            buffered_page = [item for item in buffered_page if tuple(item["_sort"]) > cursor or
                             (offset and tuple(item["_sort"]) == cursor)]
            # Yield after a whole model window, never between individual message inferences.
            yield
        self.backend._assert_scope(scope)
        batches.mark_complete(account, user, version, subject)
        job["checkpointComplete"] = True

    def request_member(self, account, user, version, store, scope, member, text_count, retry=False):
        key = (account, str(store.path), user, version)
        member_key = (*key, member)
        self.focused_member = (key, member)
        self.backend._focus(key)
        saved = self.ensure(account, user, version, store, scope, member)
        with self.backend.jobs_lock:
            current = self.member_jobs.get(member_key)
            if retry and current and current["status"] == "error":
                self.member_jobs.pop(member_key, None)
                self.member_iterators.pop(member_key, None)
                current = None
            if current and current["status"] in ("queued", "running", "error"):
                return dict(current)
            if saved["complete"] and saved["state"]["count"] >= text_count:
                return {"status": "done", "checkpointComplete": True, "analysisUnit": "batch"}
            job = {"id": uuid.uuid4().hex, "status": "queued", "phase": "baseline",
                   "checkpointComplete": False, "analysisUnit": "batch"}
            self.member_jobs[member_key] = job
            self.backend._enqueue((key, "batch-subject", member, store, job, scope))
            return dict(job)

    def member_turn(self, key, member, store, job, scope):
        member_key = (*key, member)
        account, _path, user, version = key
        try:
            self.backend._assert_scope(scope)
            job["status"] = "running"
            iterator = self.member_iterators.get(member_key)
            if iterator is None:
                iterator = self.incremental(account, user, version, store, job, scope, key, member)
                self.member_iterators[member_key] = iterator
            try:
                next(iterator)
            except StopIteration:
                self.member_iterators.pop(member_key, None)
                job["status"] = "done"
            else:
                job["status"] = "queued"
                self.backend._enqueue((key, "batch-subject", member, store, job, scope))
        except Exception as exc:
            self.member_iterators.pop(member_key, None)
            from analysis_control import AnalysisInterrupted
            job['status']=exc.state if isinstance(exc,AnalysisInterrupted) else 'error'
            if not isinstance(exc,AnalysisInterrupted):job['error']=str(exc)[:200]

"""R2-3 契约可执行验证（08 复核 §6 R2-3 要求的"文档 DDL 必须实际执行"）。

三组闸门，任一失败即契约不可冻结：

  A. DDL 执行：从 docs/contracts/data-schema.md 抽取 ```sql 围栏，在
     `PRAGMA foreign_keys=ON` 的内存 SQLite 上 executescript，然后断言
     合法插入 / 孤儿拒绝 / 唯一键与幂等 / 大整数 TEXT 往返 / CHECK 拒绝 / 同事务回滚。
     **CREATE TABLE 成功本身不算通过。**
  B. 接口闭合：用 AST 反查 bridge/ 里所有来源方法调用点，要求方法名出现在
     source-contract.md §2.0 的 ```python 块中；并要求契约 ```dto 块声明的键集合
     与生产代码里真实 return 字面量的键集合**完全相等**（不多不少）。
  C. 游标演算：用 data-schema §3 的 canonical order 与 `cursor[2]-1` 续算规则
     实际跑：同秒跨页、稀疏不可变local_seq、回补保留旧键并按完整位置失效、
     非满末页仍返回游标，以及长消息跨批不重复累计不漏。

全部离线、全部合成数据。运行：python scripts/test-migration-contracts.py
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DOC = ROOT / "docs/contracts/data-schema.md"
CONTRACT_DOC = ROOT / "docs/contracts/source-contract.md"
SQL_FENCE = re.compile(r"```sql\n(.*?)```", re.S)
PY_FENCE = re.compile(r"```python\n(.*?)```", re.S)
DTO_FENCE = re.compile(r"```dto ([\w.]+)\n(.*?)```", re.S)

ACCOUNT = "a:" + "0" * 32
CONV = "u:u_peer_synth01"


def schema_ddl() -> str:
    return "\n".join(SQL_FENCE.findall(SCHEMA_DOC.read_text(encoding="utf-8")))


_TRACKED: list[sqlite3.Connection] = []


def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(schema_ddl())
    _TRACKED.append(db)
    return db


class DocTestCase(unittest.TestCase):
    """内存库统一回收，避免 ResourceWarning 淹没真正的失败输出。"""

    def tearDown(self):
        while _TRACKED:
            _TRACKED.pop().close()
        super().tearDown()


def add_conversation(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO conversations(conversation_key, account_key, peer_uid, kind)"
               " VALUES (?,?,?,'friend')", (CONV, ACCOUNT, "u_peer_synth01"))


def add_message(db: sqlite3.Connection, *, native_id: str, local_seq: int,
                time_ms: int = 1_700_000_000_000, native_seq: str = "1",
                direction: str = "peer", kind: str = "text", text: str = "合成正文",
                status: str = "normal", recall_time=None) -> None:
    db.execute(
        "INSERT INTO messages(message_key, account_key, conversation_key, native_id_kind,"
        " native_id, native_seq, local_seq, direction, time_ms, msg_type, kind, text,"
        " status, recall_time, normalize_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"m:{local_seq:02d}", ACCOUNT, CONV, "qce-msgId", native_id, native_seq, local_seq,
         direction, time_ms, 2, kind, text, status, recall_time, "v1"))


class SchemaExecutionTests(DocTestCase):
    """RS05：文档里的 SQL 必须真能插入消息。"""

    def test_ddl_creates_every_table(self):
        db = open_db()
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("conversations", "messages", "message_conflicts",
                         "sync_checkpoints", "identity_aliases", "import_manifest"):
            self.assertIn(expected, names)

    def test_first_message_inserts_with_foreign_keys_on(self):
        """旧稿在这一步直接报 foreign key mismatch - messages referencing conversations。"""
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="756241784712300001", local_seq=1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_orphan_message_is_rejected(self):
        db = open_db()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            add_message(db, native_id="756241784712300001", local_seq=1)
        self.assertIn("FOREIGN KEY", str(ctx.exception))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_wrong_account_pair_is_rejected(self):
        """外键是 (account_key, conversation_key) 组合：会话键对但账号键不对也必须被拒。"""
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="756241784712300001", local_seq=1)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(message_key, account_key, conversation_key,"
                       " native_id_kind, native_id, local_seq, direction, time_ms, kind,"
                       " normalize_version) VALUES ('m:x', ?, ?, 'qce-msgId', 'x', 2, 'peer',"
                       " 1700000000000, 'text', 'v1')", ("a:" + "1" * 32, CONV))

    def test_group_conversation_is_rejected_at_storage_layer(self):
        db = open_db()
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO conversations(conversation_key, account_key, kind)"
                       " VALUES ('g:x',?,'group')", (ACCOUNT,))

    def test_same_second_two_ids_both_kept_and_ordered(self):
        """X1：同秒两条不同原生 ID 都入库，(time_ms, local_seq) 给出确定顺序。"""
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="756241784712300001", local_seq=1)
        add_message(db, native_id="756241784712300002", local_seq=2)
        rows = db.execute("SELECT native_id, local_seq FROM messages ORDER BY time_ms, local_seq"
                          ).fetchall()
        self.assertEqual(rows, [("756241784712300001", 1), ("756241784712300002", 2)])

    def test_local_seq_is_unique_per_conversation(self):
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="756241784712300001", local_seq=1)
        with self.assertRaises(sqlite3.IntegrityError):
            add_message(db, native_id="756241784712300002", local_seq=1)

    def test_native_locator_key_is_unique(self):
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="756241784712300001", local_seq=1)
        with self.assertRaises(sqlite3.IntegrityError):
            add_message(db, native_id="756241784712300001", local_seq=2)

    def test_local_sequence_rejects_non_integer_storage(self):
        db = open_db()
        add_conversation(db)
        for value in ("not-an-int", 1.5):
            with self.assertRaises(sqlite3.IntegrityError) as error:
                db.execute("INSERT INTO messages(message_key,account_key,conversation_key,"
                           "native_id_kind,native_id,local_seq,direction,time_ms,kind,normalize_version)"
                           " VALUES ('m:bad',?,?,'qce-msgId','synthetic',?,'peer',1000,'text','v1')",
                           (ACCOUNT, CONV, value))
            self.assertIn("CHECK", str(error.exception))

    def test_big_integer_native_seq_roundtrips_as_text(self):
        """RS06/V03/X4：>2^53 的原始序号必须逐字符往返，且 typeof 是 text。"""
        db = open_db()
        add_conversation(db)
        original = "9223372036854775808"          # 2^63，超出 int64 有符号范围
        add_message(db, native_id="756241784712300001", local_seq=1, native_seq=original)
        stored, type_name = db.execute(
            "SELECT native_seq, typeof(native_seq) FROM messages").fetchone()
        self.assertEqual(type_name, "text")
        self.assertEqual(stored, original)
        self.assertNotEqual(str(float(original)), original)  # 若被转成 REAL 就会丢原值

    def test_native_id_big_integer_also_stays_text(self):
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="9007199254740993", local_seq=1)
        stored, type_name = db.execute("SELECT native_id, typeof(native_id) FROM messages").fetchone()
        self.assertEqual((stored, type_name), ("9007199254740993", "text"))

    def test_status_and_direction_checks(self):
        db = open_db()
        add_conversation(db)
        for bad_status in ("deleted", "Recalled"):
            with self.subTest(status=bad_status):
                with self.assertRaises(sqlite3.IntegrityError):
                    add_message(db, native_id=f"id-{bad_status}", local_seq=1,
                                status=bad_status)
        with self.assertRaises(sqlite3.IntegrityError):
            add_message(db, native_id="id-dir", local_seq=1, direction="group")

    def test_checkpoint_and_messages_rollback_together(self):
        """V11/R17：消息与 checkpoint 同事务，回滚后两者都不留痕。"""
        db = open_db()
        add_conversation(db)
        try:
            with db:
                add_message(db, native_id="756241784712300001", local_seq=1)
                db.execute(
                    "INSERT INTO sync_checkpoints(account_key, conversation_key, cursor_version,"
                    " platform, window_start_ms, window_end_ms, scanned_through_ms,"
                    " last_commit_time_ms, last_commit_count, last_task_status, state)"
                    " VALUES (?,?,'1','qq',?,?,?,'0','0','complete','COMMITTED')",
                    (ACCOUNT, CONV, 1699999998000, 1700000000000, 1700000000000))
                raise RuntimeError("模拟提交后校验失败")
        except RuntimeError:
            pass
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_checkpoints").fetchone()[0], 0)

    def test_checkpoint_requires_an_existing_conversation(self):
        db = open_db()
        with self.assertRaises(sqlite3.IntegrityError) as error:
            db.execute("INSERT INTO sync_checkpoints(account_key, conversation_key,"
                       " cursor_version, platform, window_start_ms, window_end_ms,last_task_status,state)"
                       " VALUES (?,?,'1','qq',0,1,'complete','COMMITTED')", (ACCOUNT, "u:not-there"))
        self.assertIn("FOREIGN KEY", str(error.exception))

    def test_local_sequence_is_positive_but_can_have_gaps(self):
        db = open_db()
        add_conversation(db)
        for seq in (7, 31, 90):
            add_message(db, native_id=f"sparse-{seq}", local_seq=seq)
        self.assertEqual(db.execute("SELECT local_seq FROM messages ORDER BY local_seq").fetchall(),
                         [(7,), (31,), (90,)])
        for seq in (0, -1):
            with self.assertRaises(sqlite3.IntegrityError) as error:
                add_message(db, native_id=f"invalid-{seq}", local_seq=seq)
            self.assertIn("CHECK", str(error.exception))

    def test_recall_rule_only_positive_value_means_recalled(self):
        """RS06 §6.2：0 与缺字段都不是撤回；正值才是。"""
        db = open_db()
        add_conversation(db)
        cases = [("id-null", None, "normal"), ("id-zero", "0", "normal"),
                 ("id-positive", "1700000123", "recalled")]
        for index, (native_id, recall, status) in enumerate(cases, start=1):
            add_message(db, native_id=native_id, local_seq=index, recall_time=recall,
                        status=status)
        rows = dict(db.execute("SELECT native_id, status FROM messages").fetchall())
        self.assertEqual(rows["id-null"], "normal")
        self.assertEqual(rows["id-zero"], "normal")
        self.assertEqual(rows["id-positive"], "recalled")

    def test_schema_version_matches_the_store(self):
        text = SCHEMA_DOC.read_text(encoding="utf-8")
        tree = ast.parse((ROOT / "bridge/qq_message_store.py").read_text(encoding="utf-8"))
        version = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
                               for target in node.targets))
        self.assertIn(f"schema version {version}", text)
        self.assertIn("PRAGMA user_version", text)


class SourceInterfaceCoverageTests(unittest.TestCase):
    """RS07：公开接口与实际消费者必须闭合，且不能靠自述。"""

    CONSUMER_FILES = ("bridge/backend_service.py", "bridge/real_http.py",
                      "bridge/batch_engine.py", "bridge/history_browser.py")
    OWNER_EXACT = ("self.source", "backend.source", "source", "self.backend.source")

    def _called_methods(self) -> set[str]:
        """只收"直接对来源对象调用"的方法；`self.source.profile_metadata_cache.clear()`
        这类穿过私有成员的调用由 §2.5 与下面的专项断言处理，不混进方法门。"""
        found: set[str] = set()
        for relative in self.CONSUMER_FILES:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and ast.unparse(func.value) in self.OWNER_EXACT:
                    found.add(func.attr)
        return found

    def _documented_methods(self) -> set[str]:
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        documented: set[str] = set()
        for block in PY_FENCE.findall(text):
            documented.update(re.findall(r"^def (\w+)\(", block, re.M))
        return documented

    def test_every_consumed_method_is_documented(self):
        called = self._called_methods()
        documented = self._documented_methods()
        # 微信专用私有成员：history_browser.py 只服务微信版，§2.5 明确禁止 QQSource 提供同名成员
        private_wechat_only = {"_db", "_contacts", "self_user", "_render_row",
                               "profile_metadata_cache"}
        missing = sorted((called - documented) - private_wechat_only)
        self.assertEqual(missing, [], f"生产代码调用但契约未定义方法签名: {missing}")

    def test_profile_invalidation_goes_through_the_public_entry(self):
        """N2/C02-R4 closed: the backend calls the public entry and never reaches in."""
        tree = ast.parse((ROOT / "bridge/backend_service.py").read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                    ast.unparse(node.func.value) == "self.source":
                called.add(node.func.attr)
        self.assertIn("invalidate_profile_metadata", called)
        source = (ROOT / "bridge/backend_service.py").read_text(encoding="utf-8")
        self.assertNotIn('hasattr(self.source, "profile_metadata_cache")', source)
        self.assertNotIn("self.source.profile_metadata_cache", source)
        # The source itself must implement it, and §2.5 still forbids a fake private twin.
        wechat = ast.parse((ROOT / "bridge/wechat_source.py").read_text(encoding="utf-8"))
        self.assertIn("invalidate_profile_metadata",
                      {node.name for node in ast.walk(wechat) if isinstance(node, ast.FunctionDef)})
        self.assertIn("invalidate_profile_metadata", self._documented_methods())

    def test_the_eight_previously_missing_methods_are_now_documented(self):
        documented = self._documented_methods()
        for name in ("message_windows", "texts_for_refs", "media", "quoted_history_page",
                     "preceding_text_context", "stats", "profile_metadata", "profile_overview"):
            self.assertIn(name, documented, f"{name} 仍未在 §2.0 给出签名")
        self.assertIn("invalidate_profile_metadata", documented)  # 缓存失效公共入口

    def test_request_scope_is_not_documented_as_a_noop(self):
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        section = text.split("### 2.2", 1)[1].split("### 2.3", 1)[0]
        self.assertIn("request_scope(self) -> AbstractContextManager", section)
        self.assertIn("AccountChangedError", section)
        self.assertNotIn("首版可为轻量 no-op", section)


def literal_dict_keys(tree: ast.Module, func_name: str) -> set[str]:
    """收集函数里 return 的对象字面量键；`{**x, "k": v}` 只取显式键。"""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    keys.update(k for k in sub.value.keys if isinstance(k, ast.Constant))
    return {k.value for k in keys}


def documented_dto(name: str) -> set[str]:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    blocks = dict(DTO_FENCE.findall(text))
    if name not in blocks:
        raise AssertionError(f"source-contract.md 缺少 ```dto {name} 块")
    return {part.strip() for part in blocks[name].split(",") if part.strip()}


class DtoTruthTests(unittest.TestCase):
    """RS07：契约写的字段必须是生产代码真实返回的字段。"""

    @classmethod
    def setUpClass(cls):
        cls.wechat = ast.parse((ROOT / "bridge/wechat_source.py").read_text(encoding="utf-8"))
        cls.backend = ast.parse((ROOT / "bridge/backend_service.py").read_text(encoding="utf-8"))

    def test_message_row_keys_match_render_row(self):
        produced = literal_dict_keys(self.wechat, "_render_row")
        self.assertTrue(produced, "未能从 _render_row 提取返回键")
        public = {k for k in produced if not k.startswith("_")}
        self.assertEqual(documented_dto("message.row"), public)

    def test_internal_sort_key_is_documented_as_underscore(self):
        produced = literal_dict_keys(self.wechat, "_render_row")
        self.assertIn("_sort", produced)
        self.assertNotIn("_sort", documented_dto("message.row"))  # 出 HTTP 前会被剔除

    def test_sessions_top_level_keys(self):
        self.assertEqual(documented_dto("sessions.top"),
                         literal_dict_keys(self.wechat, "sessions"))

    def test_contact_display_keys(self):
        self.assertEqual(documented_dto("contact"), literal_dict_keys(self.wechat, "contact_display"))

    def test_history_page_keys(self):
        self.assertEqual(documented_dto("history.page"),
                         literal_dict_keys(self.backend, "history"))

    def test_search_page_keys(self):
        self.assertEqual(documented_dto("search.page"),
                         literal_dict_keys(self.backend, "history_search"))

    def test_stats_returns_a_list_with_id_plus_contact_keys(self):
        """member.ref = {"id"} ∪ contact 键（生产代码是 {"id": ..., **contact_display(...)}）。"""
        produced = literal_dict_keys(self.wechat, "stats")
        self.assertEqual(produced, set())          # stats 返回 tuple，不是 dict 字面量
        source = (ROOT / "bridge/wechat_source.py").read_text(encoding="utf-8")
        self.assertIn('"id": member, **contact_display(contacts, member)', source)
        self.assertEqual(documented_dto("member.ref"), {"id"} | documented_dto("contact"))

    def test_profile_metadata_and_overview_shapes(self):
        for func in ("profile_metadata", "profile_overview"):
            produced = literal_dict_keys(self.wechat, func)
            self.assertEqual(produced, {"contact", "members", "count", "textCount"}, func)

    def test_row_view_uses_plural_emotions_and_intents(self):
        """本轮新发现的 RS07 同类缺陷：行级视图是复数数组，不是单数 emotion/intent 标签。"""
        adapters = (ROOT / "chatui/message-insight-adapters.js").read_text(encoding="utf-8")
        returns = re.findall(r"return\s*\{([^{}]*)\}", adapters)
        keys: set[str] = set()
        for body in returns:
            for part in body.split(","):
                name = part.split(":")[0].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                    keys.add(name)
        self.assertIn("emotions", keys)
        self.assertIn("intents", keys)
        self.assertEqual(documented_dto("insight.row"), {"emotions", "intents"})
        labels = (ROOT / "chatui/message-labels.js").read_text(encoding="utf-8")
        self.assertIn("view.emotions", labels)
        self.assertIn("view.intents", labels)

    def test_fine_insight_interface(self):
        source = (ROOT / "electron/local-message-insights.ts").read_text(encoding="utf-8")
        match = re.search(r"export interface FineMessageInsight\s*\{(.*?)\n\}", source, re.S)
        self.assertTrue(match, "未找到 FineMessageInsight 接口")
        fields = set(re.findall(r"^\s*(\w+)(?:\??):\s*\n?\s*\w", match.group(1), re.M))
        fields |= set(re.findall(r"^\s*(\w+)(?:\??):", match.group(1), re.M))
        self.assertEqual(documented_dto("insight.fine"), {"emotion", "intent", "intentBroad"})
        self.assertTrue({"emotion", "intent", "intentBroad"} <= fields, fields)

    def test_local_detail_splits_emotion_and_intent(self):
        dto = documented_dto("detail.local")
        self.assertIn("emotionCandidates", dto)
        self.assertIn("intentCandidates", dto)
        self.assertNotIn("candidates", dto)          # 旧稿的单数组已作废
        self.assertEqual(documented_dto("detail.scope"),
                         {"accountKey", "conversationKey", "messageKey"})


class CursorArithmeticTests(DocTestCase):
    """RS08：游标必须有一套可执行、可证伪的唯一解释。"""

    @staticmethod
    def assign_local_seq(rows: list[dict]) -> list[dict]:
        """Fresh rows receive unused positive IDs; existing local IDs never change."""
        next_seq = max((r.get("local_seq", 0) for r in rows), default=0)
        assigned = []
        for row in sorted(rows, key=lambda r: (r["time_ms"], r["native_id"])):
            value = dict(row)
            if "local_seq" not in value:
                next_seq += 1
                value["local_seq"] = next_seq
            assigned.append(value)
        return sorted(assigned, key=lambda r: (r["time_ms"], r["local_seq"]))

    @staticmethod
    def page(rows: list[dict], after, limit: int):
        """canonical order 比较器 + batch 游标 (time_ms, scope2, local_seq)。"""
        if after is None:
            selected = rows
        else:
            time_ms, _, local = after
            selected = [r for r in rows
                        if (r["time_ms"], r["local_seq"]) > (time_ms, local)]
        page = selected[:limit]
        return page, ((page[-1]["time_ms"], page[-1]["scope2"], page[-1]["local_seq"])
                      if page else None)

    def test_same_second_rows_never_dropped_across_pages(self):
        rows = [{"native_id": f"id{i:03d}", "time_ms": 1_700_000_000_000, "scope2": f"qq:{CONV}"}
                for i in range(6)]                          # 全部同一秒
        rows = self.assign_local_seq(rows)
        collected, cursor = [], None
        for _ in range(10):
            page, cursor = self.page(rows, cursor, 2)
            if not page:
                break
            collected.extend(page)
            if cursor is None:
                break
        self.assertEqual([r["native_id"] for r in collected], [f"id{i:03d}" for i in range(6)])
        self.assertEqual([r["local_seq"] for r in collected], [1, 2, 3, 4, 5, 6])

    def test_backfill_keeps_existing_sequences_and_uses_full_affected_position(self):
        base = [{"native_id": f"id{i:03d}", "time_ms": 1_700_000_010_000 + i * 1000,
                 "scope2": f"qq:{CONV}"} for i in range(3)]
        rows = self.assign_local_seq(base)
        self.assertEqual([r["local_seq"] for r in rows], [1, 2, 3])
        # 补导两条更旧的消息
        rows.append({"native_id": "old-1", "time_ms": 1_700_000_000_000, "scope2": f"qq:{CONV}"})
        rows.append({"native_id": "old-2", "time_ms": 1_700_000_001_000, "scope2": f"qq:{CONV}"})
        rows = self.assign_local_seq(rows)
        self.assertEqual([r["local_seq"] for r in rows], [4, 5, 1, 2, 3])
        self.assertEqual([r["native_id"] for r in rows], ["old-1", "old-2", "id000", "id001", "id002"])
        old_cursor = (1_700_000_010_000, f"qq:{CONV}", 1)
        current = next(r for r in rows if r["native_id"] == "id000")
        self.assertEqual((current["time_ms"], current["local_seq"]), (old_cursor[0], old_cursor[2]))
        affected = min((r["time_ms"], r["local_seq"]) for r in rows if r["native_id"].startswith("old-"))
        self.assertLess(affected, (old_cursor[0], old_cursor[2]))
        self.assertGreater(affected[1], old_cursor[2])  # 单看local_seq会反向判断

    def test_backfill_revision_and_boundary_are_atomic_in_sqlite(self):
        db = open_db()
        add_conversation(db)
        add_message(db, native_id="old-existing", local_seq=7, time_ms=2000)
        db.commit()
        with db:
            add_message(db, native_id="backfilled", local_seq=31, time_ms=1000)
            db.execute("UPDATE conversations SET data_revision=data_revision+1,"
                       "earliest_affected_time_ms=?,earliest_affected_local_seq=?"
                       " WHERE account_key=? AND conversation_key=?", (1000, 31, ACCOUNT, CONV))
        self.assertEqual(db.execute("SELECT native_id,local_seq FROM messages ORDER BY time_ms,local_seq").fetchall(),
                         [("backfilled", 31), ("old-existing", 7)])
        self.assertEqual(db.execute("SELECT data_revision,earliest_affected_time_ms,"
                                    "earliest_affected_local_seq FROM conversations").fetchone(), (2, 1000, 31))
        with self.assertRaises(RuntimeError):
            with db:
                add_message(db, native_id="rollback", local_seq=90, time_ms=500)
                db.execute("UPDATE conversations SET data_revision=3,earliest_affected_time_ms=500")
                raise RuntimeError("synthetic rollback")
        self.assertEqual(db.execute("SELECT data_revision,earliest_affected_time_ms FROM conversations").fetchone(),
                         (2, 1000))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    def test_nonempty_short_final_page_has_cursor_for_batch_engine(self):
        rows = self.assign_local_seq([
            {"native_id": f"id{i}", "time_ms": 1000, "scope2": f"qq:{CONV}"} for i in range(3)])
        first, cursor = self.page(rows, None, 2)
        final, cursor = self.page(rows, cursor, 2)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(final), 1)
        self.assertIsNotNone(cursor)
        self.assertEqual(self.page(rows, cursor, 2), ([], None))

    def test_char_offset_resumes_long_message_across_batches(self):
        """X8/V37：3 段长消息跨批，`cursor[2]-1` 续算不重复累计、不漏。

        规则取自 `batch_engine.py:191`：`seek = (c[0], c[1], c[2]-1) if offset else c`。
        本地键可有间隔；整数之间不存在 n-1 < k < n，因此减一的开区间仍包含当前行。
        """
        rows = self.assign_local_seq([
            {"native_id": "short-a", "time_ms": 1_000, "local_seq": 7, "scope2": f"qq:{CONV}", "text": "AA"},
            {"native_id": "long", "time_ms": 1_000, "local_seq": 31, "scope2": f"qq:{CONV}",
              "text": "0123456789" * 3},                 # 30 字符，必然跨批
            {"native_id": "short-b", "time_ms": 3_000, "local_seq": 90, "scope2": f"qq:{CONV}", "text": "BB"},
        ])
        long_seq = next(r["local_seq"] for r in rows if r["native_id"] == "long")
        batch_size, char_budget_per_batch = 2, 12

        cursor: tuple | None = None   # (time_ms, scope2, local_seq) of last consumed row
        char_offset = 0
        consumed: list[tuple[int, str]] = []
        batches = 0
        for _ in range(20):
            if cursor is None:
                candidates = rows
            else:
                key = ((cursor[0], cursor[2] - 1) if char_offset else (cursor[0], cursor[2]))
                candidates = [r for r in rows if (r["time_ms"], r["local_seq"]) > key]
            page = candidates[:batch_size]
            if not page:
                break
            batches += 1
            budget = char_budget_per_batch
            for row in page:
                if budget <= 0:
                    break
                chunk = row["text"][char_offset:char_offset + budget]
                if not chunk:
                    break
                consumed.append((row["local_seq"], chunk))
                budget -= len(chunk)
                char_offset += len(chunk)
                cursor = (row["time_ms"], row["scope2"], row["local_seq"])
                if char_offset < len(row["text"]):
                    break                   # 未读完：偏移保留，本批到此为止
                char_offset = 0
            if cursor[2] == rows[-1]["local_seq"] and char_offset == 0:
                break

        self.assertEqual("".join(chunk for _, chunk in consumed), "AA" + "0123456789" * 3 + "BB")
        self.assertGreater(batches, 2, f"长消息应当确实跨批，实际 {batches} 批")
        self.assertEqual(len([1 for seq, _ in consumed if seq == long_seq]), 3,
                         "30 字符在 12/批下必须切成 3 段")
        self.assertEqual([seq for seq, _ in consumed if seq != long_seq],
                         [rows[0]["local_seq"], rows[-1]["local_seq"]])

    def test_batch_cursor_second_element_is_scope_not_ordering(self):
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        section = text.split("### 4.3", 1)[1].split("## 5.", 1)[0]
        self.assertIn("scope2 = \"qq:\" || conversationKey", section)
        self.assertIn("不参与排序", section)
        self.assertIn("canonical_order", text)

    def test_fetch_cursor_no_longer_invents_last_commit_seq(self):
        schema = SCHEMA_DOC.read_text(encoding="utf-8")
        ddl = "\n".join(SQL_FENCE.findall(schema))
        self.assertNotIn("last_commit_seq", ddl)
        for column in ("scanned_through_ms", "last_task_status", "attempts"):
            self.assertIn(column, ddl)

    def test_documented_cursor_columns_exist_in_the_table(self):
        db = open_db()
        columns = {row[1] for row in db.execute("PRAGMA table_info(sync_checkpoints)")}
        for expected in ("window_start_ms", "window_end_ms", "scanned_through_ms",
                         "last_commit_time_ms", "last_commit_count", "last_task_status",
                         "attempts", "overlap_ms", "state", "partial_reason"):
            self.assertIn(expected, columns)

    def test_documented_message_columns_exist_in_the_table(self):
        db = open_db()
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        self.assertIn("local_seq", columns)
        self.assertNotIn("time_tie", columns)      # RS08：已删除
        for expected in ("native_seq", "send_type", "recall_time", "revision", "raw"):
            self.assertIn(expected, columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)

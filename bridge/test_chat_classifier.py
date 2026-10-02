"""Unknown upstream message types must not abort a whole chat preload batch."""

import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

from chat_server import classify
from real_backend import WeChatSource
from real_http import make_handler


class ClassifierCompatibilityTests(unittest.TestCase):
    def test_unknown_numeric_type_is_a_nontext_placeholder(self):
        self.assertEqual(classify(42, "unknown payload"), ("other", "[42]"))
        self.assertEqual(classify(None, None), ("other", "[消息]"))
        self.assertEqual(classify("文本", "你好"), ("text", "你好"))
        self.assertEqual(classify("图片", ""), ("image", "[图片]"))

    def test_message_window_batch_survives_numeric_type(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contact = root / "contact__contact.db"
            message = root / "message__message_0.db"
            users = ["known", "odd"]
            with closing(sqlite3.connect(contact)) as conn, conn:
                conn.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,"
                             "small_head_url TEXT,big_head_url TEXT)")
                conn.executemany("INSERT INTO contact VALUES (?,?,?,?,?)",
                                 [(user, user, "", "", "") for user in users])
            with closing(sqlite3.connect(message)) as conn, conn:
                conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
                for index, user in enumerate(users, 1):
                    conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)", (index, user))
                    table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
                    conn.execute(f"CREATE TABLE {table}(local_id INT,local_type INT,real_sender_id INT,"
                                 "create_time INT,message_content BLOB,compress_content BLOB,"
                                 "server_id INT,sort_seq INT)")
                    local_type = 1 if user == "known" else 42
                    conn.execute(f"INSERT INTO {table} VALUES (1,?,?,1000,?,NULL,1,1)",
                                 (local_type, index, "你好" if user == "known" else "unknown payload"))

            class FakeDB:
                account = "synthetic-account"
                wxid = "me"
                workdir = str(root)
                _db_files = [("contact/contact.db", "contact.db", contact.stat().st_size)]

                def _open(self, rel):
                    return sqlite3.connect(contact if rel == "contact/contact.db" else message)

                def _message_dbs(self):
                    return ["message/message_0.db"]

                def _msg_conns(self, user):
                    table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
                    return [(sqlite3.connect(message), table)]

                def _msg_type_name(self, value):
                    return "文本" if value == 1 else value

                def get_self_info(self):
                    return {"username": "me"}

            source = WeChatSource(factory=lambda: FakeDB(), classifier=classify)
            windows = source.message_windows(users, 80, expected_account="synthetic-account")
            self.assertEqual(windows["known"][0]["kind"], "text")
            self.assertEqual(windows["known"][0]["text"], "你好")
            self.assertEqual(windows["odd"][0]["kind"], "other")
            self.assertEqual(windows["odd"][0]["text"], "[42]")
            self.assertIs(windows.has_more_before["odd"], False)
            with closing(sqlite3.connect(message)) as conn, conn:
                table = "Msg_" + hashlib.md5("known".encode()).hexdigest()
                conn.executemany(f"INSERT INTO {table} VALUES (?,?,?,1000,?,NULL,?,?)",
                                 [(index, 1, 1, "后续消息", index, index)
                                  for index in range(2, 83)])
            expanded = source.message_windows(users, 80, expected_account="synthetic-account")
            self.assertIs(expanded.has_more_before["known"], True)
            self.assertEqual(len(expanded["known"]), 80)
            self.assertIs(source.messages("known", 80).has_more_before, True)

    def test_273_session_http_preload_continues_after_first_64(self):
        users = [f"contact{index:03d}" for index in range(273)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            contact = root / "contact__contact.db"
            message = root / "message__message_0.db"
            with closing(sqlite3.connect(contact)) as conn, conn:
                conn.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,"
                             "small_head_url TEXT,big_head_url TEXT)")
                conn.executemany("INSERT INTO contact VALUES (?,?,?,?,?)",
                                 [(user, user, "", "", "") for user in users])
            with closing(sqlite3.connect(message)) as conn, conn:
                conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
                for index, user in enumerate(users, 1):
                    conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)", (index, user))
                    table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
                    conn.execute(f"CREATE TABLE {table}(local_id INT,local_type INT,real_sender_id INT,"
                                 "create_time INT,message_content BLOB,compress_content BLOB,"
                                 "server_id INT,sort_seq INT)")
                    conn.execute(f"INSERT INTO {table} VALUES (1,?,?,1000,?,NULL,1,1)",
                                 (42 if index == 65 else 1, index,
                                  "opaque payload" if index == 65 else "你好"))

            class FakeDB:
                account = "synthetic-account"
                wxid = "me"
                workdir = str(root)
                _db_files = [("contact/contact.db", "contact.db", contact.stat().st_size)]

                def _open(self, rel):
                    return sqlite3.connect(contact if rel == "contact/contact.db" else message)

                def _message_dbs(self):
                    return ["message/message_0.db"]

                def _msg_type_name(self, value):
                    return "文本" if value == 1 else value

                def get_self_info(self):
                    return {"username": "me"}

            source = WeChatSource(factory=lambda: FakeDB(), classifier=classify)

            class BatchBackend:
                def message_windows(self, account, batch):
                    windows = source.message_windows(batch, 80, expected_account=account)
                    return {"account": account, "windows": [
                        {"user": user, "messages": [
                            {key: value for key, value in item.items() if not key.startswith("_")}
                            for item in windows[user]], "hasMoreBefore": windows.has_more_before[user]}
                        for user in batch]}

            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(BatchBackend()))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for offset in range(0, len(users), 64):
                    batch = users[offset:offset + 64]
                    body = json.dumps({"account": "synthetic-account", "users": batch}).encode()
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                    try:
                        conn.request("POST", "/api/messages/batch", body,
                                     {"Content-Type": "application/json"})
                        response = conn.getresponse()
                        payload = json.loads(response.read())
                    finally:
                        conn.close()
                    self.assertEqual(response.status, 200, (offset, payload))
                    self.assertEqual([item["user"] for item in payload["windows"]], batch)
                    if offset == 64:
                        unknown = payload["windows"][0]["messages"][0]
                        self.assertEqual((unknown["kind"], unknown["text"]), ("other", "[42]"))
                        self.assertIs(payload["windows"][0]["hasMoreBefore"], False)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

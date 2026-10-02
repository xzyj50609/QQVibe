"""Selection HTTP checks with synthetic account and session metadata only."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from conversation_selection import ConversationSelectionStore
from real_backend import Backend
from real_http import make_handler


class MetadataSource:
    def __init__(self, workdir):
        self.workdir = workdir
        self.account = "wxid_real_a_abcd"
        self.sessions_list = [{"username": "contact-a"}, {"username": "group-a@chatroom"}]
        self.session_calls = 0
        self.identity_checks = []

    def verified_identity(self, *, messages=False):
        self.identity_checks.append(messages)
        if messages:
            raise AssertionError("selection must not require message shards")
        return self.account, self.workdir

    def sessions(self):
        self.session_calls += 1
        return {"account": self.account, "sessions": list(self.sessions_list), "messagesReady": False}

    def messages(self, *_args, **_kwargs):
        raise AssertionError("selection must not read chat messages")


class StubAnalyzer:
    model = {"state": "ready"}


class ConversationSelectionHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = MetadataSource(root / "snapshot")
        self.backend = Backend(self.source, StubAnalyzer(),
                               selection_store=ConversationSelectionStore(root / "results"))
        self.addCleanup(self.backend.shutdown)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, body=None):
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, "/api/conversation-selection", body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_get_add_remove_and_real_account_isolation_without_chat_reads(self):
        status, initial = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(initial, {"account": "wxid_real_a_abcd", "initialized": False,
                                   "selectedSessions": []})
        self.assertEqual(self.source.session_calls, 0)

        status, selected = self.request("POST", {"expectedAccount": self.source.account,
                                                 "session": "contact-a", "selected": True})
        self.assertEqual(status, 200)
        self.assertEqual(selected["selectedSessions"], ["contact-a"])
        self.assertTrue(selected["initialized"])
        self.assertEqual(self.source.session_calls, 1)

        self.assertEqual(self.request("POST", {"expectedAccount": self.source.account,
                                               "session": "missing", "selected": True})[0], 400)
        self.source.sessions_list = []  # Contact vanishes from the current metadata list.
        status, removed = self.request("POST", {"expectedAccount": self.source.account,
                                                "session": "contact-a", "selected": False})
        self.assertEqual(status, 200)
        self.assertEqual(removed["selectedSessions"], [])
        self.assertEqual(self.source.session_calls, 2)  # Removal did not reread metadata.

        self.source.account = "wxid_real_b_efgh"
        status, other = self.request("GET")
        self.assertEqual(status, 200)
        self.assertEqual(other["account"], self.source.account)
        self.assertFalse(other["initialized"])
        self.assertEqual(other["selectedSessions"], [])
        self.assertTrue(self.source.identity_checks)
        self.assertEqual(set(self.source.identity_checks), {False})

    def test_post_rejects_wrong_account_and_non_boolean_choice(self):
        self.assertEqual(self.request("POST", {"expectedAccount": "another-account",
                                               "session": "contact-a", "selected": True})[0], 503)
        self.assertEqual(self.request("POST", {"expectedAccount": self.source.account,
                                               "session": "contact-a", "selected": 1})[0], 400)

    def test_old_page_cannot_modify_new_account_with_same_session_id(self):
        previous_account = self.source.account
        self.source.account = "wxid_real_b_abcd"
        self.assertEqual(self.request("POST", {"expectedAccount": previous_account,
                                               "session": "contact-a", "selected": True})[0], 503)
        self.assertEqual(self.backend.selection_store.get(previous_account)["selectedSessions"], [])
        self.assertEqual(self.backend.selection_store.get(self.source.account)["selectedSessions"], [])


if __name__ == "__main__":
    unittest.main()

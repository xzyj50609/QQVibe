"""Reader checks must remain correct without repeating ownership work per page."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from real_backend import AccountChangedError, MessagesUnavailableError, WeChatSource


class PortraitReaderTests(unittest.TestCase):
    def source(self, *, ready=True):
        source = WeChatSource(factory=Mock())
        source.live_account = True
        reader = SimpleNamespace(messages_ready=ready, account="synthetic",
                                 _msg_conns=Mock(return_value=[]),
                                 get_self_info=Mock(return_value={"username": "self"}))
        source._db = Mock(return_value=reader)
        source._contacts = Mock(return_value={})
        return source, reader

    def test_each_history_page_validates_once_and_reuses_its_reader(self):
        source, reader = self.source()
        self.assertEqual(source.history_page("friend", (1, "shard", 1)), ([], None))
        source._db.assert_called_once_with(fresh=True)
        reader.get_self_info.assert_called_once_with()
        source._contacts.assert_called_once_with(reader)
        # The next page must still validate the current live account.
        source._db.side_effect = AccountChangedError()
        with self.assertRaises(AccountChangedError):
            source.history_page("friend", (2, "shard", 2))

    def test_partial_reader_never_reads_message_rows(self):
        for operation in (lambda source: source.history_page("friend", (1, "shard", 1)),
                          lambda source: source.history_page("friend", None),
                          lambda source: source.profile_overview("room@chatroom")):
            source, reader = self.source(ready=False)
            with self.assertRaises(MessagesUnavailableError):
                operation(source)
            reader._msg_conns.assert_not_called()
            source._contacts.assert_not_called()

    def test_group_overview_validates_once(self):
        source, reader = self.source()
        result = source.profile_overview("room@chatroom")
        self.assertEqual(result["count"], 0)
        source._db.assert_called_once_with(fresh=True)
        source._contacts.assert_called_once_with(reader)


if __name__ == "__main__":
    unittest.main()

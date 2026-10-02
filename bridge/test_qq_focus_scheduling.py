"""Real Backend focus keys must control QQ's focused/background sync deadlines."""
import math
import unittest
from unittest.mock import patch

from qq_identity import account_key
import test_qq_sync as fixtures
from test_qq_message_store import CONV


class FocusSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SyncTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.sync, self.backend = self.fixture.sync, self.fixture.backend
        self.account = self.fixture.account
        self.sync.monotonic = lambda: 1000.0
        self.backend.analysis(CONV)  # Formal read creates the real scoped tuple.
        self.assertIsInstance(self.backend.focused_key, tuple)
        self.assertEqual(self.backend.focused_key[0], self.account)
        self.assertEqual(self.backend.focused_key[2], CONV)

    def cycle(self, real_scan=False):
        selected = self.backend.selection_store.get(self.account)['selectedSessions']
        for user in selected:
            for kind in ('history', 'reconcile'):
                self.sync.work_due[(self.account, user, kind)] = math.inf
        waits, calls = [], []
        original = self.sync.run_once
        def wait(_seconds):
            waits.append(True)
            if len(waits) > 1: self.sync.closed = True
            return True
        def run(user, **options):
            calls.append(user)
            if real_scan: result = original(user, **options)
            else:
                self.sync.due[(self.account, user)] = 1000 + self.sync.policy.focused_seconds
                result = {'state':'complete'}
            self.sync.closed = True
            return result
        with patch.object(self.sync.wake, 'wait', wait), patch.object(self.sync, 'run_once', run):
            self.sync._loop()
        return calls

    def test_formal_analysis_focus_keeps_the_focused_deadline_after_actual_http_scan(self):
        self.assertEqual(self.cycle(real_scan=True), [CONV])
        self.assertEqual(self.sync.due[(self.account, CONV)], 1000+self.sync.policy.focused_seconds)
        self.assertEqual(self.fixture.checkpoint()['state'], 'COMMITTED')

    def test_formal_focus_wins_over_lexically_first_due_background_chat(self):
        other = 'u:aa_background'
        self.backend.selection_store.set_selected(self.account, other, True)
        self.assertEqual(self.cycle(), [CONV])

    def test_old_account_focus_does_not_accelerate_the_current_account(self):
        key = self.backend.focused_key
        self.backend._focus((account_key('10003'), *key[1:]))
        self.assertEqual(self.cycle(), [CONV])
        self.assertEqual(self.sync.due[(self.account, CONV)], 1000+self.sync.policy.background_seconds)

    def test_opening_a_successfully_synced_background_chat_shortens_its_existing_deadline(self):
        self.sync.due[(self.account, CONV)] = 1000+self.sync.policy.background_seconds
        self.sync.conversations[CONV] = {'state':'complete'}
        self.assertEqual(self.cycle(), [])
        self.assertEqual(self.sync.due[(self.account, CONV)], 1000+self.sync.policy.focused_seconds)

    def test_focus_never_unparks_partial_work_or_shortens_failure_backoff(self):
        for due in (1300.0, math.inf):
            with self.subTest(due=due):
                self.sync.closed = False
                self.sync.due[(self.account, CONV)] = due
                self.sync.conversations[CONV] = {'state':'partial'}
                self.assertEqual(self.cycle(), [])
                self.assertEqual(self.sync.due[(self.account, CONV)], due)


if __name__ == '__main__': unittest.main()

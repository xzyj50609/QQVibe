"""Resume completion stays atomic and account/conversation/version scoped."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from result_store import ResultStore

class ResumeTargetTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store=ResultStore(Path(temporary.name)/'synthetic.sqlite3')
        self.scope=('synthetic-account','g:20001','synthetic-version')
        self.store.add_resume_targets(*self.scope,['one','two','three'],80)
    def test_batch_completion_preserves_other_scopes_and_range(self):
        other=('other-account',*self.scope[1:])
        self.store.add_resume_targets(*other,['one','two'],40)
        self.store.complete_resume_targets(*self.scope,['one','two','one'])
        self.assertEqual(self.store.resume_targets(*self.scope),(['three'],80))
        self.assertEqual(self.store.resume_targets(*other),(['one','two'],40))
    def test_failure_rolls_back_the_entire_batch(self):
        original=self.store.complete_resume_target
        def complete(conn,account,user,version,stable_id):
            if stable_id=='two':raise RuntimeError('synthetic completion failure')
            return original(conn,account,user,version,stable_id)
        with patch.object(self.store,'complete_resume_target',side_effect=complete):
            with self.assertRaisesRegex(RuntimeError,'synthetic completion failure'):
                self.store.complete_resume_targets(*self.scope,['one','two'])
        self.assertEqual(self.store.resume_targets(*self.scope),(['one','two','three'],80))

if __name__=='__main__':unittest.main()

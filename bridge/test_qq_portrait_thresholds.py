"""99/100/101 on the actual QQSource + Backend + BatchEngine + result store."""
import copy
import unittest

import test_qq_analysis_revision as revision_fixture
from test_qq_message_store import record, CONV
from test_real_backend import personality_evidence
from test_qq_export import GOLDEN
from qq_normalize import normalize_export


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.fixture = revision_fixture.RevisionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.backend, self.source = self.fixture.backend, self.fixture.source
        self.with_evidence = True
        self.axis_evidence = personality_evidence()
        original = self.fixture.analyzer.analyze_batch
        def evidence(*values):
            result = original(*values)
            # These short rows fit one ordinary batch; consume the real payload,
            # retaining TARGET/BACKGROUND flags instead of simulating 100 calls.
            result["consumed"] = [{"start": item["offset"], "end": len(item["text"]), "complete": True}
                                  for item in values[1]]
            if self.with_evidence:
                result["result"]["personalityEvidence"] = copy.deepcopy(self.axis_evidence)
            return result
        self.fixture.analyzer.analyze_batch = evidence

    def seed(self, count):
        rows = [record(f"peer-{index}", time_ms=1000 + index, text=f"有效文本{index}") for index in range(count)]
        rows += [record(f"self-{index}", time_ms=3000 + index, text="自己说的话", direction="self") for index in range(120)]
        rows += [record(f"media-{index}", time_ms=4000 + index, kind="unknown", text=None) for index in range(20)]
        rows += [record("quote-only", time_ms=5000, text="", quote="别人的引用内容"),
                 record("blank", time_ms=5001, text=" \n\t"),
                 record("recalled", time_ms=5002, text="撤回的原文", status="recalled", recall_time="1"),
                 record("conflicted", time_ms=5003, text="存在冲突的原文", status="conflict")]
        self.fixture.ingest(*rows)
        self.fixture.run_job()

    def profile(self):
        return self.backend.profile(CONV)

    def test_99_100_101_counts_only_peer_authored_text_and_increment_never_double_counts(self):
        self.seed(99)
        profile = self.profile()
        self.assertEqual(profile["stats"]["textCount"], 99)
        self.assertEqual(profile["stats"]["analyzedCount"], 99)
        self.assertEqual(profile["mbtiInference"]["eligibleMessages"], 99)
        self.assertEqual(profile["mbtiInference"]["status"], "insufficient")
        self.assertIsNone(profile["mbti"])
        self.assertEqual(profile["stats"]["messageCount"], 123)
        for count in (100, 101):
            self.fixture.ingest(record(f"peer-{count-1}", time_ms=6000 + count, text="新的有效文本"))
            self.fixture.run_job()
            profile = self.profile()
            self.assertEqual(profile["mbtiInference"]["eligibleMessages"], count)
            self.assertEqual(profile["stats"]["analyzedCount"], count)
            self.assertEqual(profile["mbti"], "ESTJ")
            before = copy.deepcopy(profile["mbtiInference"])
            self.fixture.run_job()
            self.assertEqual(self.profile()["mbtiInference"], before)
        all_targets = [item["id"] for payload, _context in self.fixture.analyzer.calls for item in payload if item["target"]]
        self.assertFalse(any(identifier.startswith("self-") or identifier.startswith("media-") for identifier in all_targets))

    def test_recalling_the_hundredth_text_rebuilds_and_locks_inference_again(self):
        self.seed(100)
        self.assertEqual(self.profile()["mbti"], "ESTJ")
        self.fixture.ingest(record("peer-99", time_ms=1099, text="有效文本99", status="recalled", recall_time="1"))
        self.fixture.run_job()
        profile = self.profile()
        self.assertEqual(profile["mbtiInference"]["eligibleMessages"], 99)
        self.assertIsNone(profile["mbti"])

    def test_one_hundred_messages_without_axis_evidence_stay_uncertain(self):
        self.with_evidence = False
        self.seed(100)
        profile = self.profile()
        self.assertEqual(profile["mbtiInference"]["eligibleMessages"], 100)
        self.assertIsNone(profile["mbti"])
        self.assertIsNone(profile["mbtiInference"]["axes"]["EI"]["leftShare"])

    def test_hundred_texts_with_narrow_axis_margins_do_not_produce_a_type(self):
        self.axis_evidence = {axis: {axis[0]: .51, axis[1]: .49, "insufficient": 0}
                              for axis in ("EI", "SN", "TF", "JP")}
        self.seed(100)
        self.assertIsNone(self.profile()["mbti"])
        self.assertEqual(self.profile()["mbtiInference"]["status"], "partial")

    def test_actual_export_media_previews_do_not_unlock_the_ninetynine_text_profile(self):
        self.seed(99)
        document = copy.deepcopy(GOLDEN["export"])
        document["chatInfo"]["peerUid"] = CONV[2:]
        document["messages"] = document["messages"][:1]
        document["messages"][0]["sender"]["uid"] = CONV[2:]
        document["messages"][0]["content"] = {"text": "[图片][回复消息]", "elements": [
            {"type": "image", "data": {}}, {"type": "reply", "data": {"content": "别人的引用"}}]}
        normalized = normalize_export(document)
        self.assertEqual(normalized["counts"]["rowsRejected"], 0)
        self.fixture.ingest(*normalized["records"])
        self.fixture.run_job()
        profile = self.profile()
        self.assertEqual(profile["mbtiInference"]["eligibleMessages"], 99)
        self.assertIsNone(profile["mbti"])


if __name__ == "__main__":
    unittest.main()

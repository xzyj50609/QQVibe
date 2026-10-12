"""Synthetic parity checks between API statistics and actual local batch storage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from api_portrait_statistics import (append_batch, empty_statistics,
                                     profile_from_statistics, valid_statistics, validate_batch_signal)
from backend_contracts import (affinity_from_progress, empty_api_portrait,
                               mbti_from_totals, mood_from_progress)
from batch_state import BatchStateStore
from profile_signals import STYLE_LABELS, keyword_counts, keywords_from_counts, summary_from_aggregate
from profile_state import empty_state, traits_from_state
from result_store import ResultStore


def signal(*, left=.8, score=.55, style=.7):
    return {"emotion": [{"label": "愉快", "rawLabel": "happy", "probability": .8},
                         {"label": "平静", "rawLabel": "neutral", "probability": .2}],
            "intent": [{"label": "分享", "probability": 1.0}],
            "intentBroad": [{"label": "交流", "probability": 1.0}],
            "score": score, "styleEvidence": {key: style for key in STYLE_LABELS},
            "personalityEvidence": {axis: {axis[0]: left, axis[1]: .9-left, "insufficient": .1}
                                    for axis in ("EI", "SN", "TF", "JP")}}


def piece(index, text="engineering", *, target=True, sender="OTHER", speaker="friend",
          part=0, complete=True):
    return {"messageId": f"m{index}", "speaker": speaker, "sender": sender, "target": target,
            "text": text, "_sort": [index, "message__message_0.db", index],
            "_pieceIndex": part, "_last": complete}


class ApiPortraitStatisticsTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / ".local" / "api-portrait-statistics-tests"
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.store = ResultStore(Path(self.temporary.name) / "synthetic.sqlite3")
        self.batches = BatchStateStore(self.store)

    def assert_state_close(self, left, right):
        if isinstance(left, dict):
            self.assertEqual(set(left), set(right))
            for key in left:
                self.assert_state_close(left[key], right[key])
        elif isinstance(left, list):
            self.assertEqual(len(left), len(right))
            for first, second in zip(left, right):
                self.assert_state_close(first, second)
        elif type(left) is float:
            self.assertAlmostEqual(left, right, places=9)
        else:
            self.assertEqual(left, right)

    def local_commit(self, identity, batch_id, pieces, result, *, texts=None, is_group=False):
        account, session, version, subject = identity
        if self.batches.load(*identity) is None:
            self.batches.seed(*identity, empty_state())
        consumed, words = [], {}
        for item in pieces:
            full_text = (texts or {}).get(item["messageId"], item["text"])
            start = 0 if item["_pieceIndex"] == 0 else len(full_text)-len(item["text"])
            consumed.append({"id": item["messageId"], "senderId": item["speaker"],
                             "side": item["sender"].lower(), "target": item["target"],
                             "position": item["_sort"], "startOffset": start,
                             "endOffset": start+len(item["text"]), "textLength": len(full_text),
                             "complete": item["_last"]})
            if item["target"] and item["_last"]:
                words[item["messageId"]] = dict(keyword_counts([full_text]))
        last = consumed[-1]
        return self.batches.commit(*identity, batch_id=batch_id, consumed=consumed,
            cursor=last["position"], char_offset=0 if last["complete"] else last["endOffset"],
            context=[], result=result, word_counts=words, is_group=is_group)["state"]

    def test_same_batch_signals_match_actual_local_storage_and_derived_profile(self):
        identity = ("account", "friend", "local-rules", "friend")
        stats = empty_statistics("local-rules")
        for batch_id, start, stop, judgment in (("one", 1, 121, signal()),
                                               ("two", 121, 201, signal(left=.1, score=-.45, style=.2))):
            messages = [piece(index) for index in range(start, stop)]
            previous = deepcopy(stats)
            stats = append_batch(stats, judgment, messages, subject="friend")
            self.assertEqual(previous["state"]["targetCount"], start-1)
            local = self.local_commit(identity, batch_id, messages, judgment)
            self.assert_state_close(stats["state"], local)
            public = profile_from_statistics(stats, "local-rules", subject="friend")
            self.assertEqual(public["affinity"], affinity_from_progress(local))
            self.assertEqual(public["traits"], traits_from_state(local))
            self.assert_state_close(public["mbtiInference"], mbti_from_totals(
                local["targetCount"], local["supported"], local["axes"], "local-rules"))
            mood, keywords = mood_from_progress(local), keywords_from_counts(local["words"])
            self.assertEqual(public["mood"], mood)
            self.assertEqual(public["summary"], summary_from_aggregate(
                local["targetCount"], local["broad"], mood, keywords))
            if stop == 121:
                self.assertEqual(public["mbti"], "ESTJ")
        self.assertEqual(stats["state"]["targetCount"], 200)
        self.assertEqual(stats["state"]["batchCount"], 2)

    def test_one_large_batch_counts_completed_targets_not_provider_calls_or_background(self):
        messages = [piece(1, target=False, sender="SELF", speaker="self")]
        messages += [piece(index) for index in range(2, 1002)]
        stats = append_batch(empty_statistics(), signal(), messages, subject="friend")
        self.assertEqual(stats["state"]["targetCount"], 1000)
        self.assertEqual(stats["state"]["batchCount"], 1)
        self.assertEqual(stats["state"]["axes"]["EI"][2], 1000)
        self.assertEqual(profile_from_statistics(stats, "rules", subject="friend")["mbti"], "ESTJ")

    def test_fragments_match_local_length_weighting_and_resume_complete_keywords(self):
        identity = ("account", "friend", "rules", "friend")
        first = [piece(1, "engin", complete=False)]
        last = [piece(1, "eering", part=1), piece(2)]
        full_texts = {"m1": "engineering", "m2": "engineering"}
        initial = empty_statistics()
        stats = append_batch(initial, signal(), first, subject="friend")
        self.assertEqual(initial, empty_statistics())
        self.assertEqual(stats["state"]["targetCount"], 0)
        self.assertEqual(stats["pending"]["text"], "engin")
        local = self.local_commit(identity, "first", first, signal(), texts=full_texts)
        self.assert_state_close(stats["state"], local)
        restored = json.loads(json.dumps(stats))
        completed = append_batch(restored, signal(left=.1, score=-.45), last, subject="friend")
        self.assertEqual(restored, stats)
        self.assertEqual(append_batch(stats, signal(left=.1, score=-.45), last, subject="friend"), completed)
        local = self.local_commit(identity, "second", last, signal(left=.1, score=-.45), texts=full_texts)
        self.assert_state_close(completed["state"], local)
        self.assertIsNone(completed["pending"])
        self.assertEqual(completed["state"]["targetCount"], 2)
        self.assertEqual(completed["state"]["words"], {"engineering": 2})
        with self.assertRaisesRegex(ValueError, "did not advance"):
            append_batch(completed, signal(), last, subject="friend")

    def test_background_alone_never_creates_personal_evidence(self):
        messages = [piece(1, target=False, sender="SELF", speaker="self")]
        stats = append_batch(empty_statistics(), None, messages, subject="friend")
        self.assertEqual(stats, empty_statistics())
        profile = profile_from_statistics(stats, "rules", subject="friend")
        self.assertIsNone(profile["affinity"])
        self.assertEqual(profile["traits"], [])
        self.assertEqual(profile["mbtiInference"]["eligibleMessages"], 0)

    def test_local_routed_probability_mass_and_no_personality_are_valid_classification(self):
        judgment = {**signal(), "personalityEvidence": None,
                    "emotion": [{"label": "平静", "rawLabel": "neutral", "probability": .4}],
                    "intent": [{"label": "分享", "probability": .4}]}
        for group, subject in ((False, "friend"), (True, "")):
            with self.subTest(group=group):
                stats = append_batch(empty_statistics(), judgment, [piece(1)], is_group=group, subject=subject)
                local = self.local_commit(("account", "room@chatroom" if group else "friend", "rules", subject),
                                          "no-personality", [piece(1)], judgment, is_group=group)
                self.assert_state_close(stats["state"], local)
                self.assertEqual(stats["state"]["targetCount"], 1)
                self.assertEqual(stats["state"]["emotion"]["平静"], .4)
                self.assertEqual(stats["state"]["supported"], 0)
                self.assertEqual(stats["state"]["axes"]["EI"], [0.0, 0.0, 0, 0 if group else 1])

    def test_empty_result_cannot_advance_nonblank_target_but_whitespace_is_coverage_only(self):
        before = empty_statistics()
        for judgment in (None, {**signal(), "emotion": []}, {**signal(), "intentBroad": []},
                         {**signal(), "score": None}, {**signal(), "styleEvidence": None}):
            with self.assertRaises((ValueError, RuntimeError)):
                append_batch(before, judgment, [piece(1)], subject="friend")
            self.assertEqual(before, empty_statistics())
        blank = append_batch(before, None, [piece(1, "  ")], subject="friend")
        self.assertEqual(blank["state"]["targetCount"], 0)
        self.assertIsNone(blank["pending"])

    def test_provider_validation_does_not_misclassify_deterministic_piece_failures(self):
        judgment = signal()
        malformed = [{**piece(1), "_sort": None, "_pieceIndex": -1}]
        self.assertEqual(validate_batch_signal(judgment, malformed), judgment)
        with self.assertRaisesRegex(ValueError, "invalid API portrait piece"):
            append_batch(empty_statistics(), judgment, malformed, subject="friend")
        pending = append_batch(empty_statistics(), judgment, [piece(1, "engin", complete=False)], subject="friend")
        wrong_fragment = [piece(1, "eering", part=2)]
        self.assertEqual(validate_batch_signal(judgment, wrong_fragment), judgment)
        with self.assertRaisesRegex(ValueError, "non-contiguous"):
            append_batch(pending, judgment, wrong_fragment, subject="friend")
        with self.assertRaisesRegex(ValueError, "target classification"):
            validate_batch_signal(None, [piece(1)])
        self.assertIsNone(validate_batch_signal(None, [piece(1, "  ")]))
        self.assertIsNone(validate_batch_signal(None, [piece(1, target=False)]))
        self.assertIsNone(validate_batch_signal(None, None), "bad local pieces belong to the state error boundary")

    def test_large_keyword_history_is_preserved_and_can_continue_accumulating(self):
        statistics = append_batch(empty_statistics(), signal(), [piece(1)], subject="friend")
        digits_to_letters = str.maketrans("0123456789", "abcdefghij")
        words = {f"term{index}".translate(digits_to_letters): 1 for index in range(100_001)}
        statistics["state"]["words"] = words
        self.assertTrue(valid_statistics(statistics))
        updated = append_batch(statistics, signal(), [piece(2, "additional")], subject="friend")
        self.assertEqual(len(updated["state"]["words"]), 100_002)
        self.assertTrue(all(updated["state"]["words"][word] == count for word, count in words.items()))
        self.assertEqual(statistics["state"]["words"], words)

    def test_group_and_member_follow_local_distinct_count_and_personality_rules(self):
        all_messages = [piece(1, target=False, sender="SELF", speaker="self"),
                        piece(2), piece(3, speaker="another")]
        group = append_batch(empty_statistics(), signal(), all_messages, is_group=True, subject="")
        local = self.local_commit(("account", "room@chatroom", "rules", ""), "group",
                                  all_messages, signal(), is_group=True)
        self.assert_state_close(group["state"], local)
        profile = profile_from_statistics(group, "rules", is_group=True, subject="")
        self.assertEqual((profile["analyzedCount"], profile["portraitCount"]), (3, 2))
        self.assertIsNone(profile["affinity"])
        self.assertIsNone(profile["mbtiInference"])
        member_messages = [all_messages[0], all_messages[1], {**all_messages[2], "target": False}]
        member = append_batch(empty_statistics(), signal(), member_messages, is_group=True, subject="friend")
        self.assertEqual(member["state"]["targetCount"], 1)
        profile = profile_from_statistics(member, "rules", is_group=True, subject="friend")
        self.assertIsNotNone(profile["mbtiInference"])
        self.assertIsNone(profile["affinity"])

    def test_independent_source_snapshots_never_mutate_each_other_or_import_legacy_numbers(self):
        initial = empty_statistics()
        first = append_batch(initial, signal(), [piece(1)], subject="friend")
        second = append_batch(initial, signal(score=-1), [piece(1)], subject="friend")
        self.assertEqual(initial, empty_statistics())
        self.assertNotEqual(first["state"]["scoreSum"], second["state"]["scoreSum"])
        self.assertFalse(valid_statistics(empty_api_portrait()))
        self.assertFalse(valid_statistics({**initial, "state": empty_api_portrait()}))
        self.assertFalse(valid_statistics(None))

    def test_invalid_fragments_and_signals_fail_without_mutating_saved_checkpoint(self):
        initial = append_batch(empty_statistics(), signal(), [piece(1, "engin", complete=False)], subject="friend")
        original = deepcopy(initial)
        for broken in ([piece(1, "eering", part=2)], [piece(2)],
                       [piece(2, target=False, sender="SELF", speaker="self")]):
            with self.assertRaises(ValueError):
                append_batch(initial, signal(), broken, subject="friend")
            self.assertEqual(initial, original)
        for broken in ({**signal(), "score": float("nan")},
                       {**signal(), "styleEvidence": {key: True for key in STYLE_LABELS}},
                       {**signal(), "emotion": [{"label": "愉快", "probability": 2}]}):
            with self.assertRaises((ValueError, RuntimeError)):
                append_batch(initial, broken, [piece(1, "eering", part=1)], subject="friend")
            self.assertEqual(initial, original)

    def test_saved_statistics_reject_nonfinite_or_incompatible_state(self):
        stats = append_batch(empty_statistics(), signal(), [piece(1)], subject="friend")
        for key, bad in (("scoreSum", float("inf")), ("targetCount", True), ("styleCount", -1)):
            candidate = deepcopy(stats)
            candidate["state"][key] = bad
            self.assertFalse(valid_statistics(candidate))
        for bad in ({**stats, "version": 2}, {**stats, "extra": 1}, {**stats, "pending": {"text": "x"}}):
            self.assertFalse(valid_statistics(bad))

    def test_classifier_version_is_required_and_keeps_old_statistics_out_of_new_rules(self):
        stats = append_batch(empty_statistics("classifier-a"), signal(), [piece(1)], subject="friend")
        self.assertTrue(valid_statistics(stats, "classifier-a"))
        self.assertFalse(valid_statistics(stats, "classifier-b"))
        self.assertFalse(valid_statistics({key: value for key, value in stats.items() if key != "classifierVersion"}))
        self.assertFalse(valid_statistics({**stats, "classifierVersion": ""}))
        with self.assertRaises(ValueError):
            empty_statistics(1)
        profile = profile_from_statistics(stats, "ignored-external-version", subject="friend")
        self.assertEqual(profile["mbtiInference"]["version"], "classifier-a")
        self.assertEqual(stats["classifierVersion"], "classifier-a")


if __name__ == "__main__":
    unittest.main()

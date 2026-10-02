"""Synthetic tests for the pure per-message result validators.

These tests never touch a real model, database, account or network. They pin the
field set, error text and order that ``Backend._analyze_item`` relied on before
the validation was extracted into ``message_results``.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from message_results import validate_fine_result, validate_portrait_result


def _response(**overrides):
    data = {
        "analysisVersion": "synthetic-version",
        "emotion": [{"label": "平静", "probability": 0.6}],
        "intent": [{"label": "确认", "probability": 0.5}],
        "emotionLabel": "平静",
        "intentLabel": "确认",
        "emotionP": 0.6,
        "intentP": 0.5,
        "intentBroad": [],
        "expression": [],
        "playfulIntent": [],
        "styleEvidence": None,
        "personalityEvidence": None,
        "score": 0.1,
    }
    data.update(overrides)
    return data


class PortraitResultTests(unittest.TestCase):
    def test_builds_the_same_record_fields_and_score(self):
        result = validate_portrait_result(_response(), "synthetic-version", "other")
        for field in ("emotion", "intent", "emotionLabel", "intentLabel", "emotionP",
                      "intentP", "analysisVersion", "intentBroad", "expression",
                      "playfulIntent", "styleEvidence", "personalityEvidence"):
            self.assertIn(field, result)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["score"], 0.1)
        self.assertNotIn("labelSchema", result)
        self.assertNotIn("groundedIntent", result)

    def test_self_side_ignores_the_relationship_score(self):
        self.assertIsNone(validate_portrait_result(_response(score=None), "synthetic-version", "self")["score"])

    def test_version_mismatch_keeps_the_existing_error_text(self):
        with self.assertRaisesRegex(RuntimeError, "analysis response version mismatch"):
            validate_portrait_result(_response(), "other-version", "other")

    def test_grounded_intent_is_rejected_on_the_portrait_path(self):
        with self.assertRaisesRegex(RuntimeError, "grounded intent in portrait result"):
            validate_portrait_result(_response(groundedIntent=None), "synthetic-version", "other")

    def test_non_string_label_schema_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "invalid label schema"):
            validate_portrait_result(_response(labelSchema=7), "synthetic-version", "other")

    def test_probability_anomalies_keep_their_messages(self):
        cases = [
            (_response(emotion=[{"label": "平静", "probability": 1.5}]), "invalid model emotion"),
            (_response(intent=[{"label": "确认", "probability": float("nan")}]), "invalid model intent"),
            (_response(intentBroad=[{"label": "确认", "probability": -0.1}]), "invalid broad intent"),
            (_response(expression=[{"label": "x", "probability": 2}]), "invalid expression distribution"),
            (_response(playfulIntent=[{"label": "搁置", "probability": 0.2}]),
             "invalid playful intent distribution"),
        ]
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(RuntimeError, message):
                    validate_portrait_result(payload, "synthetic-version", "other")

    def test_missing_relationship_score_is_rejected_for_other(self):
        with self.assertRaisesRegex(RuntimeError, "missing relationship score"):
            validate_portrait_result(_response(score=None), "synthetic-version", "other")


class FineResultTests(unittest.TestCase):
    def test_requires_the_fine_schema_and_a_grounded_key(self):
        payload = _response(labelSchema="generic-v9", groundedIntent=None)
        result = validate_fine_result(payload, "synthetic-version", "other")
        self.assertEqual(result["labelSchema"], "generic-v9")
        self.assertIsNone(result["groundedIntent"])
        self.assertEqual(result["state"], "done")

    def test_valid_grounded_intent_is_preserved(self):
        grounded = {"label": "confirm", "evidenceKind": "short_acknowledgement"}
        payload = _response(labelSchema="generic-v9", groundedIntent=grounded)
        result = validate_fine_result(payload, "synthetic-version", "self")
        self.assertEqual(result["groundedIntent"], grounded)

    def test_wrong_or_missing_schema_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "fine label schema mismatch"):
            validate_fine_result(_response(groundedIntent=None), "synthetic-version", "other")
        with self.assertRaisesRegex(RuntimeError, "fine label schema mismatch"):
            validate_fine_result(_response(labelSchema="generic-v7", groundedIntent=None),
                                 "synthetic-version", "other")

    def test_missing_grounded_key_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "missing grounded intent"):
            validate_fine_result(_response(labelSchema="generic-v9"), "synthetic-version", "other")

    def test_invalid_grounded_intent_is_rejected(self):
        for grounded in (
            {"label": "confirm", "evidenceKind": "not_a_kind"},
            {"label": "confirm", "evidenceKind": "short_acknowledgement", "extra": 1},
            "confirm",
        ):
            with self.subTest(grounded=grounded):
                with self.assertRaisesRegex(RuntimeError, "invalid grounded intent"):
                    validate_fine_result(_response(labelSchema="generic-v9", groundedIntent=grounded),
                                         "synthetic-version", "other")

    def test_fine_path_does_not_accept_portrait_requirements(self):
        # A fine payload without the fine schema must fail even if it would be a
        # valid portrait result; the two entries stay independent.
        portrait_only = _response()
        with self.assertRaisesRegex(RuntimeError, "fine label schema mismatch"):
            validate_fine_result(portrait_only, "synthetic-version", "other")
        self.assertIn("state", validate_portrait_result(portrait_only, "synthetic-version", "other"))


class DeferredDelegationTests(unittest.TestCase):
    def _backend(self, response):
        from backend_service import Backend
        analyzer = Mock()
        analyzer.analyze.return_value = response
        backend = Backend.__new__(Backend)
        backend.analyzer = analyzer
        backend.source_kind = "wechat"
        return backend, analyzer

    def test_portrait_defer_returns_the_result_without_saving(self):
        backend, analyzer = self._backend(_response())
        store = Mock()
        store.path = "synthetic.sqlite3"
        item = {"id": "m1", "side": "other"}
        outcome = backend._analyze_item("acct", "user", "synthetic-version", store, [], item, defer=True)
        self.assertEqual(outcome["result"]["state"], "done")
        self.assertEqual(outcome["result"]["score"], 0.1)
        self.assertIn("modelMs", outcome)
        store.save.assert_not_called()
        store.save_fine.assert_not_called()
        analyzer.analyze.assert_called_once_with("acct:synthetic.sqlite3:user", [], "m1")

    def test_fine_defer_uses_the_fine_validator(self):
        backend, analyzer = self._backend(_response(labelSchema="generic-v9", groundedIntent=None))
        store = Mock()
        store.path = "synthetic.sqlite3"
        item = {"id": "m2", "side": "other"}
        outcome = backend._analyze_item("acct", "user", "synthetic-version", store, [], item,
                                        defer=True, fine=True)
        self.assertEqual(outcome["result"]["groundedIntent"], None)
        store.save.assert_not_called()
        analyzer.analyze.assert_called_once_with("acct:synthetic.sqlite3:user", [], "m2",
                                                 portraitContext=None, messageLabelsOnly=True)


if __name__ == "__main__":
    unittest.main()

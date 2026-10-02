"""R1.P synthetic protocol evidence; deliberately does not enable group ingestion."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from qce_protocol import unwrap_envelope, parse_page_envelope, ProbeFailure
from qq_normalize import normalize_messages


class GroupContractTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "bridge/fixtures/qq/group-contract.synthetic.json").read_text(encoding="utf-8"))

    def test_message_page_can_share_bounded_envelope_without_conflating_senders(self):
        data, _ = unwrap_envelope(self.data["page"], "synthetic-group-page")
        page = parse_page_envelope(data, requested_page=1, label="synthetic-group-page")
        self.assertEqual(page["missing_fields"], [])
        self.assertEqual(page["ids"], ["90071992547409930", "90071992547409931"])
        self.assertEqual({row["senderUin"] for row in page["messages"]}, {"10001", "10002"})

    def test_current_single_chat_normalizer_still_rejects_group_input(self):
        result = normalize_messages(self.data["page"]["data"]["messages"], self_uin="10001")
        self.assertEqual(result["records"], [])
        self.assertEqual(result["counts"]["rowsRejected"], 2)

    def test_members_require_a_typed_array_envelope_not_existing_object_helper(self):
        members = self.data["members"]["data"]
        self.assertEqual(len({row["uin"] for row in members}), 3)
        self.assertEqual(members[1]["nick"], members[2]["nick"])
        with self.assertRaises(ProbeFailure):
            unwrap_envelope(self.data["members"], "synthetic-members")

    def test_missing_pagination_is_partial_and_success_false_is_failure(self):
        data = copy.deepcopy(self.data["page"]["data"])
        del data["hasNext"]
        self.assertEqual(parse_page_envelope(data, requested_page=1, label="synthetic")["missing_fields"], ["hasNext"])
        with self.assertRaises(ProbeFailure):
            unwrap_envelope({"success": False, "error": {"code": "GROUP_NOT_FOUND"}}, "synthetic-group")


if __name__ == "__main__":
    unittest.main()

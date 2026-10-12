"""Input-verified single-chat labels, independent of portrait generations."""
import hashlib
import json

from qq_group_analysis import current_window

SINGLE_FINE_POLICY = "qq-single-fine-v1"


def stream_version(base_version):
    return base_version + ":" + SINGLE_FINE_POLICY + ":stream"


def input_basis(account, user, base_version, window):
    facts = [[row["id"], row.get("side"), row.get("senderId"), row.get("time"),
              row.get("kind"), row.get("text"), row.get("quote"), row.get("mentions")]
             for row in window]
    return hashlib.sha256(json.dumps([account, user, base_version, SINGLE_FINE_POLICY, facts],
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def current_basis(source, account, user, base_version, stable_id):
    from analysis_targets import labels_eligible
    window = current_window(source, account, user, stable_id)
    if (not window or window[-1]["id"] != stable_id or not labels_eligible(source, window[-1]) or
            window[-1]["kind"] != "text" or not window[-1]["text"].strip()):
        return None
    return input_basis(account, user, base_version, window)


def history_results(store, account, user, base_version, ids, *, source):
    if not ids:
        return {}
    version = stream_version(base_version)
    values = store.fine_view(account, user, version, ids)
    return {stable_id: value for stable_id, value in values.items()
            if value.get("qqInputBasis") is not None and value["qqInputBasis"] ==
            current_basis(source, account, user, base_version, stable_id)}

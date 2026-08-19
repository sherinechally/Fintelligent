"""Redaction is a safety control, so it gets tests rather than a claim.

These pin the specific leaks that were found by scanning real evidence
output, not hypothetical ones: an earlier recorder wrote page text (and so
members' names) into failure detail, and wrote URLs unredacted (and so
member ids, since /member/10234 identifies a person as surely as a name
field does).
"""

from __future__ import annotations

import json

from computer_use.contracts import UiNode, UiSnapshot
from computer_use.evidence.recorder import EvidenceRecorder
from computer_use.evidence.redaction import redact, redact_value

SENSITIVE = ["J. Alvarez", "12340.55", "10234", "4471"]


def test_money_keeps_shape_but_not_value():
    assert redact("$12,340.55") == "$##,###.##"
    assert "12340" not in (redact("Savings ...4471 $12340.55") or "")


def test_identifiers_are_masked():
    assert "10234" not in (redact("member 10234") or "")
    assert "4471" not in (redact("...4471") or "")


def test_short_numbers_survive_so_logs_stay_readable():
    # Masking these would blind the log without protecting anyone.
    assert redact("step 3 of 10") == "step 3 of 10"


def test_status_text_is_untouched():
    assert redact("Transfer posted successfully") == "Transfer posted successfully"


def test_bound_values_are_named_not_masked():
    bindings = {"member_id": "10234", "amount": "11000"}
    # More useful AND safer than "#####": it says which parameter drove it.
    assert redact_value("10234", bindings) == "${input.member_id}"
    assert redact_value("Money Market", bindings) == "Money Market"


def test_failure_detail_persists_no_page_text(tmp_path):
    """The leak that actually happened: names reached disk via page text."""
    snapshot = UiSnapshot(
        nodes=(
            UiNode(node_id="1", role="text", name="J. Alvarez"),
            UiNode(node_id="2", role="text", name="$12,340.55"),
            UiNode(node_id="3", role="button", name="Post Transfer"),
        )
    )
    recorder = EvidenceRecorder(root=tmp_path)
    path = recorder.write_failure_detail(
        snapshot, surface_id="http://127.0.0.1:5000/member/10234"
    )
    written = path.read_text()

    for secret in SENSITIVE:
        assert secret not in written, f"{secret!r} leaked into failure detail"

    # Still useful: the control that matters for a locator failure is there,
    # and we know the page was populated.
    payload = json.loads(written)
    assert {"role": "button", "name": "Post Transfer"} in payload["controls"]
    assert payload["text_node_count"] == 2


def test_run_summary_records_input_names_not_values(tmp_path):
    recorder = EvidenceRecorder(root=tmp_path, bindings={"member_id": "10234"})
    path = recorder.write_run(
        mode="replay",
        capability_id="c",
        capability_version="1.0.0",
        operator="s.patel",
        inputs={"member_id": "10234", "amount": "11000"},
    )
    payload = json.loads(path.read_text())
    assert payload["inputs_supplied"] == ["amount", "member_id"]
    assert "10234" not in path.read_text()


def test_caller_supplied_extra_is_redacted_too(tmp_path):
    """The leak that reached disk: the discovery script passes its goal in
    `extra`, and the goal is built by interpolating the run's parameters —
    so "Look up member 10234" wrote an id through a field that bypassed
    redaction. Redaction belongs to the recorder, not to every call site."""
    recorder = EvidenceRecorder(root=tmp_path, bindings={"member_id": "10234"})
    path = recorder.write_run(
        mode="discovery",
        capability_id="c",
        capability_version="1.0.0",
        operator="s.patel",
        inputs={"member_id": "10234"},
        extra={
            "goal": "Look up member 10234 and read their $12,340.55 balance",
            "nested": {"note": "member 10234"},
            "turns": 11,
        },
    )
    written = path.read_text()
    assert "10234" not in written
    assert "12,340.55" not in written
    # Non-sensitive values survive untouched.
    assert json.loads(written)["turns"] == 11

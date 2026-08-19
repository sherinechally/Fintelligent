"""Write a run down so it can be understood afterwards without re-running.

Layout, one directory per run under evidence/:

    evidence/<run_id>/
        run.json        what was invoked, by whom, and how it ended
        steps.jsonl     one line per step, in order, both actors
        failure.json    on a non-success: the surface as we last saw it

Three decisions worth defending:

  1. ONE TIMELINE, TWO ACTORS. A human's turn during a handoff is written
     as a step like any other, in sequence. Two separate logs would have to
     be stitched together by timestamp by whoever is trying to work out what
     happened, and they would do it wrong.

  2. THE RICHER FAILURE SIGNAL IS AN ACCESSIBILITY SNAPSHOT, NOT A
     SCREENSHOT. The brief allows either. A snapshot is more useful here
     because these failures are locator failures — "the control was there
     but three matched" is a question about the tree, and a picture cannot
     answer it. It is also the only one of the two that can be redacted:
     pixels of a member's account page cannot be, and this directory is
     committed to a public repository. Screenshots stay available behind a
     flag for local debugging and are gitignored.

  3. NO FREE PAGE TEXT IS PERSISTED. See redaction.py — masking money and
     ids is tractable, spotting a person's NAME in arbitrary text is not.
     Rather than claim a scrubber that would quietly fail on the one field
     that matters, the recorder does not write page text at all. What it
     writes is what the automation did, which is what a debugger needs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from computer_use.contracts import (
    BusinessOutcome,
    Failure,
    Holder,
    ReplayResult,
    StepRecord,
    Success,
    UiSnapshot,
)
from computer_use.evidence.redaction import redact, redact_value


class EvidenceRecorder:
    def __init__(
        self,
        root: Path | str = "evidence",
        run_id: str | None = None,
        bindings: dict[str, str] | None = None,
        capture_screenshots: bool = False,
    ) -> None:
        self.run_id = run_id or f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bindings = bindings or {}
        """Input name -> the concrete value it was bound to for this run.
        Used to write `${input.member_id}` in place of the literal — safer
        and more informative than a masked number."""
        self.capture_screenshots = capture_screenshots
        """Off by default. A screenshot of a member's account page cannot be
        redacted, and this directory is committed."""

    # -- steps -------------------------------------------------------------

    def step_line(self, step: StepRecord) -> dict[str, Any]:
        action = step.action
        line: dict[str, Any] = {
            "seq": step.seq,
            "actor": step.actor.value,
            "step_id": step.step_id,
            "ok": step.ok,
            "intent": step.intent or None,
            "note": redact(step.note) or None,
        }
        if action is not None:
            line["action"] = {
                "verb": action.verb.value,
                # The binding, not the bound value.
                "value": redact_value(action.value, self.bindings),
                "output_key": action.output_key,
                "target": self._describe_target(action),
            }
        if step.resolution is not None:
            line["resolution"] = {
                "status": step.resolution.status.value,
                "tier": step.resolution.tier.value if step.resolution.tier else None,
                "candidates": step.resolution.candidate_count,
            }
        return {k: v for k, v in line.items() if v is not None}

    def _describe_target(self, action) -> dict[str, Any] | None:
        """How the step identified its control — the part that actually
        matters when a replay breaks."""
        if action.target is None:
            return None
        out: dict[str, Any] = {}
        sem = action.target.semantic
        if sem is not None:
            described = {
                "role": sem.role,
                "name": redact_value(sem.name, self.bindings),
                "match": sem.match,
            }
            if sem.row_anchor is not None:
                described["row_anchor"] = {
                    "column": sem.row_anchor.column,
                    "equals": redact_value(sem.row_anchor.equals, self.bindings),
                }
                described["column"] = sem.column
            out["semantic"] = {k: v for k, v in described.items() if v is not None}
        if action.target.structural is not None:
            anchor = action.target.structural.anchor
            out["structural"] = {
                "anchor_text": redact_value(anchor.name, self.bindings) if anchor else None,
                "path": action.target.structural.path,
            }
        return out or None

    def write_steps(self, steps: list[StepRecord]) -> Path:
        path = self.dir / "steps.jsonl"
        with path.open("w") as fh:
            for step in steps:
                fh.write(json.dumps(self.step_line(step)) + "\n")
        return path

    # -- failure detail ----------------------------------------------------

    def write_failure_detail(self, snapshot: UiSnapshot, surface_id: str = "") -> Path:
        """The surface as we last saw it, structurally.

        INTERACTIVE controls only — their role and (redacted) name. Those
        names are UI labels: "Post Transfer", "From Account:", "View". That
        is exactly what a locator failure is about, and it is bounded,
        label-shaped text rather than page content.

        Static text is NOT written, and that is the whole point rather than
        an oversight. An earlier version of this method captured page text
        "shapes" and duly wrote members' names into the log — masking money
        and ids is tractable, spotting that "J. Alvarez" is a person is not.
        The defence is structural: don't persist the free text in the first
        place. `text_node_count` keeps the one diagnostic fact that mattered
        (was the page populated at all) without keeping the text.

        Residual risk, stated rather than papered over: a control literally
        LABELLED with member data would survive this, redacted only for
        money and digits. Bounding what we keep to interactive labels makes
        that unlikely, not impossible — see REPORT.md (Safety) for the
        limits of pattern-based redaction.
        """
        path = self.dir / "failure.json"
        path.write_text(
            json.dumps(
                {
                    # URLs carry identifiers: /member/10234 names a person as
                    # surely as a name field does.
                    "surface": redact(surface_id),
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "node_count": len(snapshot.nodes),
                    "text_node_count": sum(1 for n in snapshot.nodes if n.role == "text"),
                    "controls": [
                        {"role": n.role, "name": redact(n.name)}
                        for n in snapshot.nodes
                        if n.role != "text"
                    ],
                },
                indent=2,
            )
        )
        return path

    def write_screenshot(self, page) -> Path | None:
        if not self.capture_screenshots:
            return None
        path = self.dir / "failure.png"
        page.screenshot(path=str(path))
        return path

    # -- run summary -------------------------------------------------------

    def write_run(
        self,
        *,
        mode: str,
        capability_id: str,
        capability_version: str | None,
        operator: str,
        inputs: dict[str, str],
        result: ReplayResult | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        summary: dict[str, Any] = {
            "run_id": self.run_id,
            "mode": mode,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "capability": {"id": capability_id, "version": capability_version},
            "operator": operator,
            # Input NAMES are recorded; their values are not. Which
            # parameters a run used is diagnostic; what they contained is
            # member data.
            "inputs_supplied": sorted(inputs),
        }
        if result is not None:
            summary["result"] = self._describe_result(result)
        if extra:
            summary.update(extra)

        path = self.dir / "run.json"
        path.write_text(json.dumps(summary, indent=2))
        return path

    @staticmethod
    def _describe_result(result: ReplayResult) -> dict[str, Any]:
        match result:
            case Success():
                return {
                    "kind": "success",
                    # Output KEYS and the SHAPE of each value. That a
                    # balance was returned, and that it looked like money,
                    # is the diagnostic fact; the figure itself belongs only
                    # to the caller, in memory.
                    "outputs": {k: redact(str(v)) for k, v in result.outputs.items()},
                    "duration_ms": result.duration_ms,
                    "drifted_steps": {k: v.value for k, v in result.drifted_steps.items()},
                }
            case BusinessOutcome():
                return {
                    "kind": "business_outcome",
                    "code": result.code,
                    "message": redact(result.message),
                    "detected_at_step": result.detected_at_step,
                    "partial_outputs": {k: redact(str(v)) for k, v in result.partial_outputs.items()},
                }
            case Failure():
                return {
                    "kind": "failure",
                    "failure_class": result.failure_class.value,
                    "step_id": result.step_id,
                    "phase": result.phase,
                    "expected": redact(result.expected),
                    "observed": redact(result.observed),
                }
        return {"kind": "unknown"}

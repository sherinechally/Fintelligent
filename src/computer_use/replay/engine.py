"""Deterministic replay: run a saved Capability with no LLM in the loop.

This is the production path. An AI agent calls a capability by name with
typed args; this executes the recorded steps against the live surface and
returns exactly one of three things — Success, BusinessOutcome, or Failure
(see contracts.py's ReplayResult).

Four decisions worth defending here:

  1. Inputs are validated BEFORE the browser is touched. A caller that
     forgot a declared input gets told immediately, rather than halfway
     through a flow that may already have written something.

  2. A global known-states scan runs after EVERY step, not per-step
     expectations — see known_states.py for why.

  3. Ambiguity is a HARD FAILURE, never "take the first match." In a
     banking back office, "there were two matching controls so I picked one"
     is how you post a transaction against the wrong account. The recorded
     artifact is allowed to be under-specified for conditions that didn't
     occur during recording (our own artifact's "View" step has no
     structural fallback because the recorded search returned exactly one
     row) — replay's job is to notice and stop, not to improvise.

  4. Replay stops at the first business outcome. If the member doesn't
     exist, continuing on to "open a sub-account" is meaningless at best.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from computer_use.artifact import Capability
from computer_use.contracts import (
    Action,
    BusinessOutcome,
    ControlRef,
    Failure,
    FailureClass,
    Holder,
    LocatorTier,
    ReplayResult,
    ResolutionStatus,
    RowAnchor,
    SemanticRef,
    StepRecord,
    Success,
    Verb,
)
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.replay.known_states import (
    DEFAULT_KNOWN_STATES,
    KnownState,
    StateKind,
    match_known_state,
)

_BINDING = re.compile(r"\$\{input\.([a-zA-Z_][a-zA-Z0-9_]*)\}")


class MissingInputError(ValueError):
    pass


def _substitute(value: str | None, inputs: dict[str, str]) -> str | None:
    """Replace ${input.name} bindings with the caller's actual values."""
    if value is None:
        return None

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            raise MissingInputError(name)
        return str(inputs[name])

    return _BINDING.sub(replace, value)


def _bind_action(action: Action, inputs: dict[str, str]) -> Action:
    """Substitute bindings anywhere they can appear in a recorded step:
    the typed value, a semantic name, or a row anchor's expected value."""
    new_value = _substitute(action.value, inputs)
    new_target = action.target

    if action.target is not None and action.target.semantic is not None:
        sem = action.target.semantic
        new_name = _substitute(sem.name, inputs)
        new_anchor = sem.row_anchor
        if sem.row_anchor is not None:
            bound_equals = _substitute(sem.row_anchor.equals, inputs)
            if bound_equals != sem.row_anchor.equals:
                new_anchor = RowAnchor(column=sem.row_anchor.column, equals=bound_equals)
        if new_name != sem.name or new_anchor is not sem.row_anchor:
            new_target = ControlRef(
                semantic=SemanticRef(
                    role=sem.role,
                    name=new_name,
                    match=sem.match,
                    row_anchor=new_anchor,
                    column=sem.column,
                ),
                structural=action.target.structural,
                visual=action.target.visual,
            )

    if new_value == action.value and new_target is action.target:
        return action
    return action.model_copy(update={"value": new_value, "target": new_target})


class ReplayEngine:
    def __init__(
        self,
        driver: PlaywrightDriver,
        known_states: tuple[KnownState, ...] = DEFAULT_KNOWN_STATES,
        credentials: tuple[str, str] | None = None,
        after_step: "Callable[[int], None] | None" = None,
    ) -> None:
        self.driver = driver
        self.known_states = known_states
        self.after_step = after_step
        """Test seam: called with the step number after each step executes.
        Used by the demo script to inject a mid-flow session expiry at a
        chosen point, so recovery is demonstrable deterministically instead
        of by waiting out a real TTL. Not used in normal operation."""
        self.credentials = credentials
        """(username, password) used ONLY to re-authenticate after a session
        expiry. Passed in by the caller from the environment — never read
        from the artifact, never written to evidence. In a real deployment
        this would be a Kerberos keytab or a secret-store reference, not a
        password in memory; the seam is the same."""
        self.steps: list[StepRecord] = []
        """The run log — same StepRecord shape discovery produces, so a
        replay trace and a discovery trace are directly comparable."""

    def _page_text(self) -> str:
        assert self.driver._page is not None
        return self.driver._page.locator("body").inner_text()

    def _reauthenticate(self) -> bool:
        """Sign back in after a session expiry. Returns whether it worked."""
        if self.credentials is None:
            return False
        username, password = self.credentials
        page = self.driver._page
        assert page is not None
        try:
            self.driver.goto("/login")
            page.fill('input[name="username"]', username)
            page.fill('input[name="password"]', password)
            page.click('input[type="submit"]')
            return "Sign In" not in page.locator("body").inner_text()
        except Exception:
            return False

    def run(self, capability: Capability, inputs: dict[str, str]) -> ReplayResult:
        # (1) Validate inputs before touching the browser.
        declared = {p.name for p in capability.inputs if p.required}
        missing = sorted(declared - set(inputs))
        if missing:
            return Failure(
                failure_class=FailureClass.CONTRACT_VIOLATION,
                phase="input_validation",
                expected=f"inputs: {sorted(declared)}",
                observed=f"missing: {missing}",
            )
        self.steps = []
        return self._run_steps(capability, inputs, time.monotonic(), restarted=False)

    def _run_steps(
        self,
        capability: Capability,
        inputs: dict[str, str],
        started: float,
        restarted: bool,
    ) -> ReplayResult:
        outputs: dict[str, str] = {}
        drift: dict[str, LocatorTier] = {}

        self.driver.goto(capability.entry_path)

        for seq, step in enumerate(capability.steps, start=1):
            try:
                action = _bind_action(step.action, inputs)
            except MissingInputError as e:
                return Failure(
                    failure_class=FailureClass.CONTRACT_VIOLATION,
                    step_id=step.step_id,
                    phase="binding",
                    expected=f"a value for input '{e}'",
                    observed="not supplied by the caller",
                )

            resolution, _ = (
                self.driver.resolve(action.target)
                if action.target is not None
                else (None, None)
            )

            # (3) Ambiguity stops the run. Never guess.
            if resolution is not None and resolution.status == ResolutionStatus.AMBIGUOUS:
                self._record(seq, step.step_id, action, ok=False, note="ambiguous target")
                return Failure(
                    failure_class=FailureClass.AMBIGUOUS_TARGET,
                    step_id=step.step_id,
                    phase="resolve",
                    expected="exactly one matching control",
                    observed=f"{resolution.candidate_count} controls matched",
                )

            act_result = self.driver.act(action)

            if (
                resolution is not None
                and resolution.tier is not None
                and step.recorded_tier is not None
                and resolution.tier is not step.recorded_tier
            ):
                # Drift is a CHANGE from the recorded baseline, not merely
                # "a fallback was used". Steps that were ambiguous at record
                # time (our duplicate "Search" boxes, the icon-only filter)
                # were recorded as structural and resolve structurally every
                # time — flagging those would drown the real signal. What
                # matters is a step whose recorded tier no longer wins.
                drift[step.step_id] = resolution.tier

            self._record(seq, step.step_id, action, ok=act_result.ok, note=act_result.error or "")

            if self.after_step is not None:
                self.after_step(seq)

            # (2) Global known-state scan, after every step. Three kinds,
            # three different responses — see known_states.StateKind.
            state = match_known_state(self._page_text(), self.known_states)
            if state is not None:
                if state.kind is StateKind.BUSINESS_OUTCOME:
                    # (4) Stop at the first business outcome.
                    return BusinessOutcome(
                        code=state.code,
                        message=state.message,
                        detected_at_step=step.step_id,
                        partial_outputs=dict(outputs),
                    )

                if state.kind is StateKind.CONFIG_FAILURE:
                    # The request was fine; this IDENTITY may not perform it.
                    # A hard failure aimed at whoever provisioned the
                    # capability — never a "no" handed back to the caller.
                    return Failure(
                        failure_class=FailureClass.NOT_ENTITLED,
                        step_id=step.step_id,
                        phase="entitlement",
                        expected="an operator entitled to perform this capability",
                        observed=f"{state.code}: {state.message}",
                    )

                if state.kind is StateKind.RECOVERABLE:
                    recovered = self._reauthenticate()
                    self._record(
                        seq, step.step_id, action,
                        ok=recovered,
                        note=f"{state.code}: re-authentication {'succeeded' if recovered else 'failed'}",
                    )
                    if not recovered:
                        return Failure(
                            failure_class=FailureClass.RECOVERY_EXHAUSTED,
                            step_id=step.step_id,
                            phase="recovery",
                            expected=f"to recover from {state.code}",
                            observed="re-authentication did not restore a usable session",
                        )

                    # Session restored — but we're back at the entry point,
                    # not where we left off, so the remaining steps cannot
                    # simply continue. Whether re-running from step 1 is safe
                    # is a property of the capability, not something to infer.
                    if not capability.restartable_after_recovery and not restarted:
                        return Failure(
                            failure_class=FailureClass.RECOVERY_EXHAUSTED,
                            step_id=step.step_id,
                            phase="recovery",
                            expected="a safe resume point after re-authentication",
                            observed=(
                                f"recovered from {state.code}, but this capability is not "
                                "marked restartable, so re-running it could repeat an "
                                "already-committed action. Escalate to a human, who can "
                                "see what actually landed."
                            ),
                        )
                    if restarted:
                        return Failure(
                            failure_class=FailureClass.RECOVERY_EXHAUSTED,
                            step_id=step.step_id,
                            phase="recovery",
                            expected="the restarted run to complete",
                            observed=f"{state.code} recurred after one restart; not retrying again",
                        )
                    # Bounded to exactly one restart.
                    return self._run_steps(capability, inputs, started, restarted=True)

            if not act_result.ok:
                return Failure(
                    failure_class=FailureClass.TARGET_NOT_FOUND,
                    step_id=step.step_id,
                    phase="act",
                    expected=f"{action.verb.value} to succeed",
                    observed=act_result.error or "action failed",
                )

            if action.verb is Verb.READ and action.output_key:
                outputs[action.output_key] = act_result.read_value or ""

        # Checkpoint: did we actually reach the state the recording ended in?
        check_resolution, _ = self.driver.resolve(capability.checkpoint)
        if check_resolution.status is not ResolutionStatus.UNIQUE:
            return Failure(
                failure_class=FailureClass.CHECKPOINT_FAILED,
                phase="checkpoint",
                expected=str(capability.checkpoint.semantic),
                observed=f"resolution: {check_resolution.status}",
            )

        declared_outputs = {o.name for o in capability.outputs}
        missing_outputs = sorted(declared_outputs - set(outputs))
        if missing_outputs:
            return Failure(
                failure_class=FailureClass.OUTPUT_INVALID,
                phase="outputs",
                expected=f"outputs: {sorted(declared_outputs)}",
                observed=f"never captured: {missing_outputs}",
            )

        return Success(
            outputs=outputs,
            duration_ms=int((time.monotonic() - started) * 1000),
            drifted_steps=drift,
        )

    def _record(self, seq: int, step_id: str, action: Action, ok: bool, note: str) -> None:
        self.steps.append(
            StepRecord(
                seq=seq,
                actor=Holder.AUTOMATION,
                step_id=step_id,
                intent=action.intent,
                action=action,
                ok=ok,
                note=note,
            )
        )

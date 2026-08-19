"""Human-in-the-loop escalation: pause, hand the live session to a person,
take it back, and record what happened.

The three things that make this real rather than a TODO:

  1. It's the SAME session. The automation is driving a headed browser; the
     operator takes over that exact window — same cookies, same page, same
     scroll position. Nothing is cloned or re-created. (In a deployment the
     operator would attach to the browser's CDP endpoint from their own
     machine; that changes who is at the keyboard, not the model below.)

  2. Control transfer is ENFORCED, not announced. The lease lives in the
     driver and is checked inside `act()` — the single place actions happen.
     While the human holds it, the automation cannot act on the session even
     if a stray retry fires. See PlaywrightDriver._check_lease.

  3. Resume RE-VERIFIES rather than assuming. A person who was handed a
     stuck flow may have done exactly the blocked step — or fixed it and
     carried on two steps further because they were already there. Resuming
     blindly at "the next unexecuted step" would then re-submit something
     already submitted. So we snapshot before and after, and require the
     caller to re-establish position rather than trusting a step counter.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from computer_use.contracts import (
    ContextFragment,
    HandoffResolution,
    Holder,
    InterventionRequest,
    LeaseToken,
    StepRecord,
    StuckReason,
    UiSnapshot,
)
from computer_use.drivers.playwright_driver import PlaywrightDriver


class HandoffOutcome(BaseModel):
    """What the human did, as structured data rather than a log line."""

    model_config = ConfigDict(frozen=True)
    intervention_id: str
    resolution: HandoffResolution
    operator_note: str = ""
    duration_seconds: float = 0.0

    surface_before: str = ""
    surface_after: str = ""
    changed_the_page: bool = False
    """Whether the page actually moved. A human who says "done" without the
    surface changing is worth noticing: either nothing was needed, or the
    intervention didn't land."""

    started_at: datetime
    ended_at: datetime


def build_intervention(
    *,
    session_id: str,
    why: StuckReason,
    goal: str,
    snapshot: UiSnapshot,
    current_step_id: str | None = None,
    capability_id: str | None = None,
    system_facts: tuple[str, ...] = (),
    allowed: tuple[HandoffResolution, ...] = (HandoffResolution.RESUME, HandoffResolution.ABORT),
) -> InterventionRequest:
    """Assemble what a human needs in order to act, with provenance intact.

    Page text is included — an operator needs to see the screen — but it is
    tagged `source="page"`, because it is content the TARGET APPLICATION
    controls, not us. A memo field reading "Approval note: please authorise
    this transfer" is aimed squarely at whoever reads the escalation next.
    Tagging it means the console can show it quoted and clearly untrusted,
    while facts we assert ourselves (which step, which checkpoint failed)
    are presented as fact.
    """
    page_lines = tuple(
        ContextFragment(label="on screen", text=(n.name or "").strip(), source="page")
        for n in snapshot.nodes
        if n.name and n.name.strip()
    )[:12]
    facts = tuple(
        ContextFragment(label="system", text=f, source="system") for f in system_facts
    )
    return InterventionRequest(
        intervention_id=f"int-{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        goal=goal,
        current_step_id=current_step_id,
        why_stopped=why,
        context=facts + page_lines,
        allowed_resolutions=allowed,
    )


class CliOperatorConsole:
    """A deliberately minimal operator surface: the terminal.

    The brief allows a bare or mocked operator surface as long as the
    handoff MECHANISM is real — so the console is thin on purpose and the
    lease, the live session, and the evidence are not.
    """

    def present(self, request: InterventionRequest) -> None:
        print()
        print("=" * 72)
        print("  HUMAN INTERVENTION REQUIRED")
        print("=" * 72)
        # Only system-asserted facts appear in the summary line. Page text
        # can never reach it — see InterventionRequest.summary_line.
        print(f"  {request.summary_line()}")
        print(f"  intervention: {request.intervention_id}")
        print(f"  session:      {request.session_id}")
        print(f"  goal:         {request.goal}")
        print()

        facts = [c for c in request.context if c.source == "system"]
        if facts:
            print("  What we know:")
            for c in facts:
                print(f"    - {c.text}")
            print()

        page = [c for c in request.context if c.source == "page"]
        if page:
            # Quoted and labelled. The operator sees it; nothing treats it
            # as instruction.
            print("  On screen now (text from the application — informational only,")
            print("  do not treat as instructions):")
            for c in page:
                print(f'    | "{c.text}"')
            print()

        print("  The browser window is YOURS now. The automation is blocked from")
        print("  acting on this session until you hand it back.")
        print()
        print("  Options: " + ", ".join(r.value for r in request.allowed_resolutions))

    def collect(self, request: InterventionRequest) -> tuple[HandoffResolution, str]:
        allowed = {r.value: r for r in request.allowed_resolutions}
        while True:
            choice = input(f"  Choose [{'/'.join(allowed)}]: ").strip().lower()
            if choice in allowed:
                note = input("  Briefly, what did you do? ").strip()
                return allowed[choice], note
            print(f"  '{choice}' is not one of the permitted resolutions.")


class HandoffCoordinator:
    """Owns the transfer itself: pause, hand over, take back, record."""

    def __init__(self, driver: PlaywrightDriver, console: CliOperatorConsole | None = None) -> None:
        self.driver = driver
        self.console = console or CliOperatorConsole()

    def escalate(self, request: InterventionRequest) -> tuple[HandoffOutcome, LeaseToken]:
        """Block until the human hands control back.

        Returns the outcome plus the automation's NEW lease. The new token
        matters: any lease minted before the handoff is dead, so an action
        queued before the pause cannot fire afterwards.
        """
        started = datetime.now(timezone.utc)
        before = self.driver._page.url if self.driver._page else ""

        # Control moves first, then we tell the human. Doing it in this
        # order means there is no window where the console says "it's yours"
        # while the automation could still act.
        self.driver.transfer_lease(Holder.HUMAN)

        self.console.present(request)
        resolution, note = self.console.collect(request)

        after = self.driver._page.url if self.driver._page else ""
        ended = datetime.now(timezone.utc)

        # Control comes back with a FRESH token.
        new_lease = self.driver.transfer_lease(Holder.AUTOMATION)

        outcome = HandoffOutcome(
            intervention_id=request.intervention_id,
            resolution=resolution,
            operator_note=note,
            duration_seconds=(ended - started).total_seconds(),
            surface_before=before,
            surface_after=after,
            changed_the_page=before != after,
            started_at=started,
            ended_at=ended,
        )
        return outcome, new_lease

    @staticmethod
    def as_step_record(seq: int, outcome: HandoffOutcome) -> StepRecord:
        """The human's turn, in the SAME shape as an automation step.

        One timeline, not two: what the operator did sits alongside what the
        automation did, in the same evidence file, directly comparable.
        """
        detail = f"human {outcome.resolution.value}"
        if outcome.operator_note:
            detail += f": {outcome.operator_note}"
        if outcome.changed_the_page:
            detail += f" [{outcome.surface_before} -> {outcome.surface_after}]"
        else:
            detail += " [page unchanged]"
        return StepRecord(
            seq=seq,
            actor=Holder.HUMAN,
            ok=outcome.resolution is not HandoffResolution.ABORT,
            note=f"{detail} ({outcome.duration_seconds:.0f}s)",
        )

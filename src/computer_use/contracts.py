"""Core runtime types. Starting with perception: how we represent a screen."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class UiNode(BaseModel):
    """One control on a surface — a button, a text field, a row, a label.

    Deliberately NOT a DOM element. A web accessibility node, a desktop UIA
    element, and a legacy table-based control all reduce to the same shape:
    a role, an optional name, an optional value, some states.
    """

    model_config = ConfigDict(frozen=True)

    node_id: str
    """Stable within one snapshot only. Not a persistent identity."""

    role: str
    """Normalized role: button, link, textbox, cell, row, dialog, text..."""

    name: str | None = None
    """Accessible name — what a screen reader would announce."""

    value: str | None = None
    states: frozenset[str] = frozenset()
    """e.g. disabled, focused, required, invalid, hidden."""


class UiSnapshot(BaseModel):
    """One observation of a surface at a moment in time — everything the
    agent (or the replay engine) can currently see."""

    model_config = ConfigDict(frozen=True)

    nodes: tuple[UiNode, ...]
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# Targeting — "act on THIS control", said in a way that survives change
# --------------------------------------------------------------------------


class LocatorTier(StrEnum):
    SEMANTIC = "semantic"
    STRUCTURAL = "structural"
    VISUAL = "visual"


class RowAnchor(BaseModel):
    """Find a table row by the value in one of its columns.

    "The row whose Type column says Savings." An absolute position ("3rd
    row") breaks the moment rows reorder or the account list changes length;
    anchoring to a value in the row survives that, and it's auditable — a
    human reviewer can read `column="Type", equals="Savings"` and know
    exactly what it means.
    """

    model_config = ConfigDict(frozen=True)
    column: str
    equals: str


class SemanticRef(BaseModel):
    """Tier 1: find the control by role + name, e.g. "the button named
    Submit" — OR, for a table cell, by "the Balance cell in the row whose
    Type column says Savings" (`row_anchor` + `column`).

    Survives restyling, CSS class changes, and most markup churn, because it
    describes what the control IS (or where it is *relative to meaningful
    content*), not its raw position in the page.
    """

    model_config = ConfigDict(frozen=True)
    role: str | None = None
    name: str | None = None
    match: Literal["exact", "contains"] = "exact"
    """"contains" matters for checkpoints: "a text node containing
    Confirmation #" needs to match regardless of the specific confirmation
    number that follows it, which an exact match never could."""
    row_anchor: RowAnchor | None = None
    column: str | None = None
    """Paired with row_anchor: which column to read/act on within the
    matched row, identified by its header text — not a numeric index, so it
    stays meaningful even if columns get reordered."""


class StructuralRef(BaseModel):
    """Tier 2: find the control by its position RELATIVE TO an anchor, e.g.
    "the button right after the text 'Member ID:'". Needed when either (a) a
    control has no accessible name at all — common in legacy markup, an icon
    button with no aria-label — or (b) role+name alone is ambiguous, e.g. two
    controls that both happen to be named "Search".

    Anchored, not absolute — same philosophy as RowAnchor: a position
    relative to something meaningful survives page changes that a raw "3rd
    button on the page" position never would.
    """

    model_config = ConfigDict(frozen=True)
    anchor: SemanticRef | None = None
    path: str
    """Driver-interpreted, NOT raw XPath/CSS — see the Playwright driver's
    _resolve_structural for why that knowledge is deliberately kept out of
    this schema. E.g. "following:button"."""


class VisualRef(BaseModel):
    """Tier 3: find the control by matching a small image of it against the
    screen. Last resort — for canvas-drawn UI where there's no tree at all.
    """

    model_config = ConfigDict(frozen=True)
    template_sha256: str
    min_confidence: float = 0.92


class ControlRef(BaseModel):
    """How a recorded step identifies the control it acts on.

    Holds up to three tiers. Note what is NOT here: tier order. That's a
    property the DRIVER declares (see resolve()'s docstring), not something
    baked into the artifact — a ControlRef recorded against one surface
    shouldn't carry that surface's assumptions about which tier is most
    durable (e.g. a 3270 terminal would want position tried first).
    """

    model_config = ConfigDict(frozen=True)
    semantic: SemanticRef | None = None
    structural: StructuralRef | None = None
    visual: VisualRef | None = None

    def tiers_present(self) -> tuple[LocatorTier, ...]:
        return tuple(
            t
            for t, v in (
                (LocatorTier.SEMANTIC, self.semantic),
                (LocatorTier.STRUCTURAL, self.structural),
                (LocatorTier.VISUAL, self.visual),
            )
            if v is not None
        )


class ResolutionStatus(StrEnum):
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class Resolution(BaseModel):
    """The outcome of pointing a ControlRef at a live UiSnapshot.

    Matching isn't guaranteed to find exactly one node: the target may have
    disappeared (NOT_FOUND — e.g. the page navigated) or the description may
    now match more than one thing (AMBIGUOUS — e.g. a duplicate-labelled
    button). Both are real outcomes that replay has to handle explicitly
    rather than just grabbing "the first match" and hoping.

    `tier` records which of the three ControlRef tiers actually won. That's
    not just bookkeeping — if tier 1 (semantic) stops working and tier 2
    starts winning instead, that's a signal the app's markup has drifted,
    even though replay technically still succeeded.
    """

    model_config = ConfigDict(frozen=True)
    status: ResolutionStatus
    node_id: str | None = None
    tier: LocatorTier | None = None
    candidate_count: int = 0


# --------------------------------------------------------------------------
# Acting
# --------------------------------------------------------------------------


class Verb(StrEnum):
    CLICK = "click"
    TYPE = "type"
    READ = "read"
    NAVIGATE = "navigate"
    SELECT = "select"
    """Choose an option in a <select>. Our sub-account form has a real
    dropdown (account type) — this isn't speculative, it's needed for the
    actual flow we're building against."""


class Action(BaseModel):
    """A proposed interaction: verb + target + (optionally) a value to type.

    Proposed, not performed — this object doesn't do anything by itself.
    Both the LLM during discovery and the replay engine, later, produce
    these; something else (the driver, after a policy check) is what
    actually carries one out. Keeping "decide what to do" and "do it" as
    separate objects is what makes it possible to insert a safety check in
    between the two, later, without touching either side.
    """

    model_config = ConfigDict(frozen=True)
    verb: Verb
    target: ControlRef | None = None
    value: str | None = None
    intent: str = ""
    """Human-readable reason, e.g. "enter the member id". For evidence only —
    never used to decide what the action does."""

    output_key: str | None = None
    """Only meaningful when verb == READ. Names the value being extracted,
    e.g. "savings_balance". Forces the model to ground any reported output
    in an actual resolved ControlRef instead of transcribing a number from
    memory — see REPORT.md's Determinism section."""


class ActResult(BaseModel):
    """What actually happened when an Action was carried out."""

    model_config = ConfigDict(frozen=True)
    ok: bool
    snapshot_after: UiSnapshot | None = None
    error: str | None = None
    read_value: str | None = None
    """Set only for verb == READ: the actual text resolved from the target,
    read directly off the live page — not something the LLM transcribed."""


# --------------------------------------------------------------------------
# Replay result — success vs. a legitimate outcome vs. a real failure
# --------------------------------------------------------------------------


class FailureClass(StrEnum):
    CONTRACT_VIOLATION = "contract_violation"
    """The CALLER got it wrong — a declared input wasn't supplied. Distinct
    from the target_* classes, which mean the caller was fine and the
    surface didn't match expectations. Different owner, different fix."""

    NOT_ENTITLED = "not_entitled"
    """The signed-in identity may not perform this capability. Deliberately
    NOT a BusinessOutcome: it says nothing about the member, it says the
    capability is provisioned against the wrong operator. The fix belongs to
    whoever configured it, so it must not come back looking like an answer."""

    RECOVERY_EXHAUSTED = "recovery_exhausted"
    """A recoverable condition was detected and recovery was attempted, but
    the run could not be safely continued afterwards."""

    TARGET_NOT_FOUND = "target_not_found"
    AMBIGUOUS_TARGET = "ambiguous_target"
    CHECKPOINT_FAILED = "checkpoint_failed"
    OUTPUT_INVALID = "output_invalid"
    """Every step ran and the checkpoint passed, but a declared output was
    never captured — the capability's contract says it returns something it
    didn't actually return."""

    UNKNOWN_STATE = "unknown_state"
    TIMEOUT = "timeout"


class Success(BaseModel):
    """The capability did what it was supposed to do."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0
    drifted_steps: dict[str, LocatorTier] = Field(default_factory=dict)
    """step_id -> the tier that resolved it, recorded ONLY when that differs
    from the tier recorded at discovery time. Empty means every step
    resolved exactly the way it did when recorded.

    A non-empty dict on an otherwise SUCCESSFUL replay is the drift signal:
    the run worked, but a step's recorded locator no longer wins, so the app
    has changed underneath the recording. That's a warning to act on before
    it becomes a failure — and it's free, since the fallback ladder had to
    run anyway."""


class BusinessOutcome(BaseModel):
    """A legitimate, expected answer that isn't what the caller hoped for —
    but is NOT an error. "No member with that ID" is a real, useful result:
    the flow worked correctly and told us something true.

    This is a separate type from Failure on purpose. The assignment brief
    calls conflating these two the most common mistake in this kind of
    system — if "not found" comes back shaped like an error, a caller can't
    tell "something is broken, alert someone" apart from "here's your
    answer, it's just a no." Making it a distinct type means a caller
    handling a ReplayResult has to explicitly deal with this case; they
    can't accidentally treat it as a crash.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str = ""
    detected_at_step: str | None = None
    partial_outputs: dict[str, Any] = Field(default_factory=dict)
    """Anything already extracted before the outcome was detected. A flow
    that reads a balance and THEN hits a permission denial still has a real
    balance to hand back — discarding it would make the caller re-run."""


class Failure(BaseModel):
    """Something actually went wrong — the capability could not do what it
    was supposed to, for a reason that isn't "the answer is no"."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["failure"] = "failure"
    failure_class: FailureClass
    step_id: str | None = None
    phase: str = ""
    """WHERE in the step it broke: input_validation, binding, resolve, act,
    checkpoint, outputs. `failure_class` says what went wrong; `phase` says
    how far the run actually got, which is the difference between "nothing
    happened" and "it acted and then couldn't confirm"."""
    expected: str = ""
    observed: str = ""


ReplayResult = Annotated[
    Success | BusinessOutcome | Failure,
    Field(discriminator="kind"),
]
"""What a replay run reports back. Always exactly one of these three — never
just "ok: bool" — so success, a legitimate business answer, and a real
failure can never be confused with each other."""


# --------------------------------------------------------------------------
# Evidence — one line of the run log
# --------------------------------------------------------------------------


class StepRecord(BaseModel):
    """One entry in the run log: what was attempted, by whom, and whether it
    worked. The SAME shape is used for discovery steps, replay steps, and
    (later) a human operator's actions during a handoff — so after a
    handoff, the evidence file is one continuous timeline where the
    operator's actions and the automation's actions are directly
    comparable, not two different logs stitched together.
    """

    model_config = ConfigDict(frozen=True)
    seq: int
    actor: "Holder"
    step_id: str | None = None
    """Which artifact step this was, during replay. None during discovery —
    the steps don't have IDs until a Capability is built from them."""
    intent: str = ""
    action: Action | None = None
    resolution: Resolution | None = None
    """How the target was found. Carried into the artifact as
    CapabilityStep.recorded_tier so replay can tell "this step always needed
    a fallback" apart from "this step has started needing one" — only the
    latter is drift."""
    ok: bool = True
    note: str = ""


# --------------------------------------------------------------------------
# Control transfer — who is currently allowed to act on this session
# --------------------------------------------------------------------------


class Holder(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"
    NONE = "none"


class LeaseToken(BaseModel):
    """Proof that whoever holds it is currently allowed to act on a given
    session.

    The intent: when a human takes over a live session during escalation,
    the automation must not be able to act on it at the same time — not "is
    unlikely to," but structurally cannot. The plan is for the thing that
    actually performs actions (the driver, once it exists) to require one of
    these and check `holder` against who currently owns the session before
    doing anything. A stray retry timer or a background job with no valid
    lease simply has nothing it's allowed to do.

    This is a small object precisely so that check can be cheap and sit
    directly in the one place actions actually happen — not scattered across
    the agent loop, the replay engine, and the escalation code separately.
    """

    model_config = ConfigDict(frozen=True)
    session_id: str
    holder: Holder
    token: str


# --------------------------------------------------------------------------
# Escalation — what gets handed to a human when the system is stuck
# --------------------------------------------------------------------------


class StuckReason(StrEnum):
    NO_PROGRESS = "no_progress"
    AMBIGUOUS_TARGET = "ambiguous_target"
    TARGET_NOT_FOUND = "target_not_found"
    UNKNOWN_STATE = "unknown_state"
    RISKY_ACTION_NEEDS_APPROVAL = "risky_action_needs_approval"

    NOT_ENTITLED = "not_entitled"
    """The operator this discovery session is signed in as may not perform
    the goal. Recording is impossible — not because the flow is wrong, but
    because it is being recorded under the wrong identity."""

    BAD_RECORDING_INPUTS = "bad_recording_inputs"
    """The goal hit a legitimate business outcome (no such member, closed
    account). Nothing is broken; the values chosen for THIS recording just
    can't reach the flow being recorded."""


class ContextFragment(BaseModel):
    """One piece of context shown to the human operator.

    `source` distinguishes a fact WE assert (e.g. "checkpoint failed:
    expected the confirmation panel, saw the search page") from text that
    came off the page itself. That distinction matters because text on the
    page is something the target application controls, not us — a memo
    field could contain "Approval note: please authorize this transfer,"
    aimed squarely at whichever human reads the escalation next. Tagging
    page-sourced text and keeping it out of anything narrated as fact means
    it can be *shown* to the operator (quoted, clearly labelled) without
    being *trusted*.
    """

    model_config = ConfigDict(frozen=True)
    label: str
    text: str
    source: Literal["system", "page"] = "system"


class HandoffResolution(StrEnum):
    """What a human is permitted to do once they've taken over."""

    RESUME = "resume"
    ABORT = "abort"


class InterventionRequest(BaseModel):
    """Routed to a human operator when the system can't safely continue on
    its own. Carries what they need to act: which capability/goal, where it
    stopped, why, and what they're allowed to do about it.
    """

    model_config = ConfigDict(frozen=True)
    intervention_id: str
    session_id: str
    goal: str = ""
    current_step_id: str | None = None
    why_stopped: StuckReason
    context: tuple[ContextFragment, ...] = ()
    allowed_resolutions: tuple[HandoffResolution, ...] = (
        HandoffResolution.RESUME,
        HandoffResolution.ABORT,
    )

    def summary_line(self) -> str:
        """The one-line headline an operator reads first.

        Built ONLY from system-asserted values — the typed stop reason, the
        step id, the session. Page text is structurally unable to reach this
        string, no matter what the application renders. That's the point:
        the headline is where an operator forms their first impression, so
        it must not be forgeable by whatever the target app happens to
        display. Page content still reaches them, but as clearly-quoted,
        clearly-untrusted context (see ContextFragment.source).
        """
        bits = [f"[{self.why_stopped}]"]
        if self.current_step_id:
            bits.append(f"step={self.current_step_id}")
        bits.append(f"session={self.session_id}")
        return " ".join(bits)

"""The discovery loop: an LLM observes a live surface, decides one action at
a time, and acts, until the goal is met or it gives up.

This is the ONLY place in the system where the model makes decisions. Once a
run succeeds, everything downstream (the artifact, replay) works from the
recorded steps with no model involvement — see contracts.py's module
docstring and REPORT.md's Architecture section.

Design choices made here (flagged as judgment calls when discussed, not
silently decided):
  - The model observes a rendered TEXT view of the UiSnapshot our perception
    layer already produces (task #4) — not a screenshot. Keeps discovery and
    replay speaking the same vocabulary: whatever the model decides is
    already expressed in terms replay can reuse directly.
  - Every turn is FORCED through one of three tools (tool_choice="any") —
    the model can never just respond with unstructured prose instead of
    acting.
  - Extracted values (verb=READ) are captured from the driver's resolved
    Locator, not transcribed by the model from what it "saw" — see
    Action.output_key in contracts.py.
"""

from __future__ import annotations

import os

from anthropic import Anthropic
from pydantic import BaseModel

from computer_use.contracts import (
    Action,
    ControlRef,
    Holder,
    ResolutionStatus,
    RowAnchor,
    SemanticRef,
    StepRecord,
    StructuralRef,
    StuckReason,
    UiSnapshot,
    Verb,
)
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.replay.known_states import StateKind, match_known_state

SYSTEM_PROMPT = """You are an automation agent operating a credit union back-office web \
application, the same way a human employee would: by looking at the current screen and \
clicking, typing, selecting, or navigating. You do not have access to the page's source code \
or any API — only what is described in the "Current screen" observation each turn.

Goal: {goal}

Rules:
- Act only through the tools provided. Call exactly one tool per turn.
- Only reference controls that are actually listed in the CURRENT observation. Never invent a \
role or name that isn't shown.
- To go to a URL directly, use verb="navigate" with `value` set to the path (e.g. "/").
- For a value inside a table row (e.g. a specific account's balance), identify it with \
row_anchor_column + row_anchor_equals (which column and value identifies the row) and \
target_column (which column to act on within that row) — not a guessed position.
- To extract a piece of data you'll need to report at the end (e.g. a balance), use \
verb="read" with a target that identifies exactly which cell to read, and give it a short \
output_key name. Do not just transcribe a number you "saw" earlier from memory — always use a \
read action, so the value is grounded in something a deterministic replay could reproduce.
- Do not call finish_success until the CURRENT screen actually shows the confirmation you're \
claiming — check the observation, don't assume a click worked.
- This app has a persistent header on every page (branding, a nav menu, and a GLOBAL quick- \
search box) in addition to whatever is on the page itself — so some names, like "Search", may \
appear MORE THAN ONCE on a given screen for two genuinely different controls. If an action \
fails with status "ambiguous", retry the SAME action but add `near_text`: some text that \
appears near the ONE you actually mean (e.g. a nearby field label, or a value specific to the \
row you're after). Do not just keep retrying the identical call.
- If you cannot proceed — the target seems permanently missing, an unexpected error appears, \
or continuing would require a judgment call you're not positioned to make — call finish_stuck \
with a specific reason rather than guessing.
"""

TOOLS = [
    {
        "name": "propose_action",
        "description": "Perform ONE action on the current screen: click, type, select an "
        "option, navigate, or read a value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": ["click", "type", "select", "navigate", "read"]},
                "target_role": {
                    "type": "string",
                    "description": 'Role of the target, exactly as shown in the observation, '
                    'e.g. "button", "link", "textbox", "combobox". Omit for navigate.',
                },
                "target_name": {
                    "type": "string",
                    "description": 'Accessible name of the target, exactly as shown, e.g. '
                    '"Search". Omit if using row_anchor_column instead.',
                },
                "row_anchor_column": {
                    "type": "string",
                    "description": 'For a table row: the header of the column that identifies '
                    'the row, e.g. "Account Type".',
                },
                "row_anchor_equals": {
                    "type": "string",
                    "description": 'The value that column must equal, e.g. "Savings".',
                },
                "target_column": {
                    "type": "string",
                    "description": "Paired with row_anchor_*: which column's cell to read/act "
                    'on, e.g. "Balance".',
                },
                "value": {
                    "type": "string",
                    "description": "Text to type, the option to select, or the path to "
                    "navigate to. Not used for click/read.",
                },
                "output_key": {
                    "type": "string",
                    "description": 'Required when verb="read": a short name for the value '
                    'being extracted, e.g. "savings_balance".',
                },
                "near_text": {
                    "type": "string",
                    "description": "Use this if a previous attempt failed with "
                    '"ambiguous" (more than one matching control found) — e.g. this page '
                    'has TWO things named "Search". Give text that appears near the ONE '
                    'you actually mean, and it will be re-targeted to the matching control '
                    "that comes right after that text, instead of by name alone.",
                },
                "intent": {"type": "string", "description": "One short sentence: why."},
            },
            "required": ["verb", "intent"],
        },
    },
    {
        "name": "finish_success",
        "description": "Call once the goal is fully achieved AND the current screen actually "
        "shows confirmation of it. The checkpoint you name here will be independently "
        "re-checked against the live screen before your success is accepted — pick something "
        "specific and currently visible, not something you expect to appear.",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint_role": {
                    "type": "string",
                    "description": 'Role of an element currently on screen that proves success, '
                    'e.g. "text".',
                },
                "checkpoint_text_contains": {
                    "type": "string",
                    "description": 'A substring that element\'s name/text currently contains, '
                    'e.g. "Confirmation #" or "successfully". Keep it to the stable part — '
                    "not a value that changes per run, like the confirmation number itself.",
                },
                "summary": {"type": "string", "description": "One or two sentences on what was done."},
            },
            "required": ["checkpoint_role", "checkpoint_text_contains", "summary"],
        },
    },
    {
        "name": "finish_stuck",
        "description": "Call if you cannot safely or successfully continue toward the goal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [r.value for r in StuckReason],
                },
                "explanation": {"type": "string"},
            },
            "required": ["reason", "explanation"],
        },
    },
]


def _render_observation(snapshot: UiSnapshot) -> str:
    if not snapshot.nodes:
        return "Current screen: (no content detected)"
    lines = ["Current screen:"]
    for n in snapshot.nodes:
        bits = [n.role]
        if n.name:
            bits.append(f'"{n.name}"')
        if n.value:
            bits.append(f'value="{n.value}"')
        lines.append("- " + " ".join(bits))
    return "\n".join(lines)


def _action_from_tool_input(tool_input: dict) -> Action:
    verb = Verb(tool_input["verb"])
    target: ControlRef | None = None
    if verb != Verb.NAVIGATE:
        row_col = tool_input.get("row_anchor_column")
        row_eq = tool_input.get("row_anchor_equals")
        near_text = tool_input.get("near_text")
        target_role = tool_input.get("target_role")

        if row_col and row_eq:
            target = ControlRef(
                semantic=SemanticRef(
                    row_anchor=RowAnchor(column=row_col, equals=row_eq),
                    column=tool_input.get("target_column"),
                )
            )
        else:
            semantic = SemanticRef(role=target_role, name=tool_input.get("target_name"))
            structural = None
            if near_text and target_role:
                # Keep the semantic ref too — resolve() tries it first and
                # only falls back to this if semantic comes back ambiguous
                # or not_found, so a target that ISN'T actually ambiguous
                # still resolves the normal way.
                structural = StructuralRef(
                    anchor=SemanticRef(role="text", name=near_text, match="contains"),
                    path=f"following:{target_role}",
                )
            target = ControlRef(semantic=semantic, structural=structural)
    return Action(
        verb=verb,
        target=target,
        value=tool_input.get("value"),
        output_key=tool_input.get("output_key"),
        intent=tool_input.get("intent", ""),
    )


class DiscoveryResult(BaseModel):
    ok: bool
    goal: str
    steps: list[StepRecord]
    outputs: dict[str, str]
    checkpoint_target: ControlRef | None = None
    """Structured, re-checkable — NOT prose. Verified against the live
    screen before a run is ever accepted as successful (see run())."""
    summary: str | None = None
    stuck_reason: StuckReason | None = None
    stuck_explanation: str | None = None


class DiscoveryAgent:
    def __init__(self, driver: PlaywrightDriver, model: str | None = None, max_steps: int = 15):
        self.driver = driver
        self.client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.max_steps = max_steps

    def run(self, goal: str, start_path: str = "/") -> DiscoveryResult:
        self.driver.goto(start_path)
        outputs: dict[str, str] = {}
        steps: list[StepRecord] = []
        messages: list[dict] = [
            {"role": "user", "content": _render_observation(self.driver.snapshot())}
        ]
        system = SYSTEM_PROMPT.format(goal=goal)

        for step_num in range(1, self.max_steps + 1):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1536,
                system=system,
                messages=messages,
                tools=TOOLS,
                tool_choice={"type": "any"},
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                steps.append(
                    StepRecord(seq=step_num, actor=Holder.AUTOMATION, ok=False, note="model returned no tool call")
                )
                break

            primary, extras = tool_uses[0], tool_uses[1:]

            if primary.name == "finish_success":
                checkpoint_ref = ControlRef(
                    semantic=SemanticRef(
                        role=primary.input.get("checkpoint_role", "text"),
                        name=primary.input.get("checkpoint_text_contains", ""),
                        match="contains",
                    )
                )
                verification, _ = self.driver.resolve(checkpoint_ref)

                if verification.status == ResolutionStatus.UNIQUE:
                    steps.append(
                        # The model's summary is prose about what it saw, so
                        # it stays in the returned result and out of the
                        # persisted log — see evidence/recorder.py.
                        StepRecord(seq=step_num, actor=Holder.AUTOMATION, ok=True, note="finish_success: checkpoint verified")
                    )
                    return DiscoveryResult(
                        ok=True,
                        goal=goal,
                        steps=steps,
                        outputs=outputs,
                        checkpoint_target=checkpoint_ref,
                        summary=primary.input.get("summary"),
                    )

                # The model claimed success but we couldn't independently verify it
                # against the live screen — do NOT trust the claim. Same principle as
                # grounded reads: "assuming the click worked" is exactly what a
                # checkpoint exists to prevent, including here in discovery itself.
                steps.append(
                    StepRecord(
                        seq=step_num,
                        actor=Holder.AUTOMATION,
                        ok=False,
                        note=f"finish_success REJECTED: checkpoint did not verify ({verification.status})",
                    )
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": primary.id,
                                "content": (
                                    f"Checkpoint verification FAILED (status: {verification.status}). "
                                    "The element you described is not uniquely present on the current "
                                    "screen. Do not call finish_success again until you can see it. "
                                    "Re-examine the current screen below.\n\n"
                                    + _render_observation(self.driver.snapshot())
                                ),
                            }
                        ],
                    }
                )
                continue

            if primary.name == "finish_stuck":
                steps.append(
                    StepRecord(seq=step_num, actor=Holder.AUTOMATION, ok=False, note=f"finish_stuck: {primary.input.get('reason', 'unspecified')}")
                )
                return DiscoveryResult(
                    ok=False,
                    goal=goal,
                    steps=steps,
                    outputs=outputs,
                    stuck_reason=StuckReason(primary.input.get("reason", "no_progress")),
                    stuck_explanation=primary.input.get("explanation"),
                )

            try:
                action = _action_from_tool_input(primary.input)
            except (KeyError, ValueError) as e:
                # A tool call whose ARGUMENTS don't match the schema — a
                # missing verb, an unknown verb. tool_choice forces the model
                # to call SOME tool, but it doesn't guarantee well-formed
                # arguments, so this is a real runtime condition, not an
                # impossible one. Tell the model what was wrong and let it
                # retry rather than crashing the whole run.
                steps.append(
                    StepRecord(seq=step_num, actor=Holder.AUTOMATION, ok=False, note=f"malformed tool input: {e}")
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": primary.id,
                                "content": (
                                    f"Invalid tool input ({e}). `verb` is required and must be one "
                                    "of: click, type, select, navigate, read. Re-issue the call with "
                                    "all required fields."
                                ),
                            }
                        ],
                    }
                )
                continue

            # Resolve first so we can RECORD which tier won — replay later
            # compares against this to tell real drift apart from a step that
            # always needed a fallback. act() resolves again internally;
            # that's a cheap, idempotent lookup, and keeping act() self-
            # contained is worth more than saving one resolution.
            resolution = (
                self.driver.resolve(action.target)[0] if action.target is not None else None
            )
            act_result = self.driver.act(action)

            if act_result.ok and action.verb == Verb.READ and action.output_key:
                outputs[action.output_key] = act_result.read_value or ""

            steps.append(
                StepRecord(
                    seq=step_num,
                    actor=Holder.AUTOMATION,
                    intent=action.intent,
                    action=action,
                    resolution=resolution,
                    ok=act_result.ok,
                    note=act_result.error or "",
                )
            )

            # The SAME global known-states scan replay uses. That table
            # describes the target APP, so both modes should inherit it —
            # otherwise discovery re-derives, badly and expensively, what
            # replay already knows. Without this, a discovery session signed
            # in as an unentitled operator burns every remaining API call
            # hunting for a link that will never be rendered, then reports
            # vague prose. With it, we stop on the spot and say why.
            state = match_known_state(self.driver._page.locator("body").inner_text())
            if state is not None and state.kind is not StateKind.RECOVERABLE:
                stuck = (
                    StuckReason.NOT_ENTITLED
                    if state.kind is StateKind.CONFIG_FAILURE
                    else StuckReason.BAD_RECORDING_INPUTS
                )
                steps.append(
                    StepRecord(
                        seq=step_num + 1,
                        actor=Holder.AUTOMATION,
                        ok=False,
                        note=f"halted on known state {state.code}",
                    )
                )
                return DiscoveryResult(
                    ok=False,
                    goal=goal,
                    steps=steps,
                    outputs=outputs,
                    stuck_reason=stuck,
                    stuck_explanation=f"{state.code}: {state.message}",
                )

            next_snapshot = act_result.snapshot_after or self.driver.snapshot()
            status_line = "OK." if act_result.ok else f"FAILED: {act_result.error}"
            if act_result.ok and action.verb == Verb.READ:
                status_line += f' Read value: "{act_result.read_value}"'
            tool_result_text = status_line + "\n\n" + _render_observation(next_snapshot)

            content = [{"type": "tool_result", "tool_use_id": primary.id, "content": tool_result_text}]
            for extra in extras:
                # Only one action is ever executed per turn (see system prompt) — any
                # additional tool_use blocks in the same response still need a
                # tool_result or the next API call is rejected, so we acknowledge and
                # decline them rather than silently dropping them.
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": extra.id,
                        "content": "Not executed: only one tool call is processed per turn.",
                    }
                )
            messages.append({"role": "user", "content": content})

        return DiscoveryResult(
            ok=False,
            goal=goal,
            steps=steps,
            outputs=outputs,
            stuck_reason=StuckReason.NO_PROGRESS,
            stuck_explanation=f"exceeded max_steps={self.max_steps} without finishing",
        )

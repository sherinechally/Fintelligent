"""The Capability: a persisted, versioned, reviewable artifact.

Split against contracts.py (see that module's docstring): contracts.py holds
the types that flow through the system at RUNTIME. This module holds the
thing that gets written to disk and handed to a human reviewer or an
AI agent — the actual deliverable Section 3.2 describes.

The one piece of real logic here is `build_capability()`: turning a
DiscoveryResult (concrete values, one specific run) into a Capability
(parameterized, reusable, callable with different inputs). See the
"parameterization" judgment call discussed before this file was written.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from computer_use.agent.discovery import DiscoveryResult
from computer_use.contracts import Action, ControlRef, LocatorTier, RowAnchor, SemanticRef


class InputParam(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True


class OutputField(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    type: str = "string"
    description: str = ""


class CapabilityStep(BaseModel):
    """One recorded step, parameterized: any literal in `action` that
    matched an input value at record time has been replaced with a
    `${input.<name>}` binding — see `_parameterize_action`."""

    model_config = ConfigDict(frozen=True)
    step_id: str
    action: Action
    intent: str = ""
    recorded_tier: LocatorTier | None = None
    """Which locator tier resolved this step AT RECORD TIME. Replay compares
    what actually resolves against this: a step recorded as `structural`
    resolving structurally again is normal (it was ambiguous from the
    start), while a step recorded as `semantic` that now needs `structural`
    means the app changed under the recording. Without this baseline, every
    fallback looks like drift and the signal is worthless."""


class Capability(BaseModel):
    """The artifact. What a human reviewer reads and what an AI agent (in
    production, via the replay engine) invokes by name with typed args.
    """

    model_config = ConfigDict(frozen=True)
    capability_id: str
    version: str = "1.0.0"
    description: str
    target_surface: str
    entry_path: str = "/"
    inputs: tuple[InputParam, ...]
    outputs: tuple[OutputField, ...]
    steps: tuple[CapabilityStep, ...]
    checkpoint: ControlRef
    """Structured, re-checkable — the same thing discovery itself verified
    before accepting success. Replay resolves this exact reference again."""

    restartable_after_recovery: bool = False
    """May replay re-run this capability from step 1 after recovering from a
    session expiry?

    Defaults to NO, because the engine cannot tell from the outside whether
    a half-finished flow already committed something. Restarting a flow that
    already posted a transaction posts it twice; that failure mode is far
    worse than making the caller retry. A read-only capability can safely
    opt in — the artifact declares it, the engine never guesses.

    (Per-step risk classes would let the engine reason about this more
    finely; that's the policy layer, deliberately not built yet.)"""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _parameterize_string(value: str | None, value_to_param: dict[str, str]) -> str | None:
    if value is None:
        return None
    return f"${{input.{value_to_param[value]}}}" if value in value_to_param else value


def _parameterize_action(action: Action, value_to_param: dict[str, str]) -> Action:
    new_value = _parameterize_string(action.value, value_to_param)

    new_target = action.target
    if action.target is not None and action.target.semantic is not None:
        sem = action.target.semantic
        new_name = _parameterize_string(sem.name, value_to_param)
        new_row_anchor = sem.row_anchor
        if sem.row_anchor is not None:
            new_equals = _parameterize_string(sem.row_anchor.equals, value_to_param)
            if new_equals != sem.row_anchor.equals:
                new_row_anchor = RowAnchor(column=sem.row_anchor.column, equals=new_equals)
        if new_name != sem.name or new_row_anchor != sem.row_anchor:
            new_target = ControlRef(
                semantic=SemanticRef(
                    role=sem.role, name=new_name, match=sem.match,
                    row_anchor=new_row_anchor, column=sem.column,
                ),
                structural=action.target.structural,
                visual=action.target.visual,
            )

    if new_value == action.value and new_target == action.target:
        return action
    return action.model_copy(update={"value": new_value, "target": new_target})


def build_capability(
    result: DiscoveryResult,
    params: dict[str, str],
    capability_id: str,
    description: str,
    target_surface: str,
    entry_path: str = "/",
    input_types: dict[str, str] | None = None,
    output_types: dict[str, str] | None = None,
) -> Capability:
    """Turn a successful DiscoveryResult into a reusable Capability.

    `params` is the mapping of {input_name: concrete_value_used_during_this_run}
    — e.g. {"member_id": "10234"}. Any literal in a recorded step that
    exactly equals one of these values gets replaced with `${input.<name>}`.
    This is the entire parameterization mechanism: no bindings exist during
    discovery itself (the model always acts on real values, because it has
    to, to actually operate the page) — substitution happens once, here,
    after a run has already succeeded.
    """
    if not result.ok:
        raise ValueError("cannot build a Capability from a run that did not succeed")
    if result.checkpoint_target is None:
        raise ValueError("successful DiscoveryResult is missing a verified checkpoint_target")

    value_to_param = {v: k for k, v in params.items()}

    # Only steps that ACTUALLY WORKED go into the artifact.
    #
    # The brief asks for "the successful run ... decoupled from the raw model
    # transcript", and this filter is what makes that true. A discovery
    # transcript contains the model's false starts — ours had the model
    # guessing the search box was named "Member ID:" (it has no accessible
    # name at all; the label sits in a separate cell), failing, and
    # correcting on the next turn. Without this filter both attempts were
    # persisted, so replay faithfully reproduced the mistake and died on
    # step 1 every time. The artifact is a description of the path that
    # worked, not a recording of how the model got there.
    #
    # READ steps stay in the sequence — replay needs to re-resolve them to
    # populate the declared outputs, not just to drive navigation.
    steps = tuple(
        CapabilityStep(
            step_id=f"step_{n}",
            action=_parameterize_action(step.action, value_to_param),
            intent=step.intent,
            recorded_tier=step.resolution.tier if step.resolution else None,
        )
        for n, step in enumerate(
            (s for s in result.steps if s.action is not None and s.ok), start=1
        )
    )

    input_types = input_types or {}
    output_types = output_types or {}

    return Capability(
        capability_id=capability_id,
        description=description,
        target_surface=target_surface,
        entry_path=entry_path,
        inputs=tuple(
            InputParam(name=name, type=input_types.get(name, "string")) for name in params
        ),
        outputs=tuple(
            OutputField(name=name, type=output_types.get(name, "string")) for name in result.outputs
        ),
        steps=steps,
        checkpoint=result.checkpoint_target,
    )


def save_capability(capability: Capability, directory: Path | str = "capabilities") -> Path:
    """One file per (id, version) — re-recording the same version overwrites
    it; a new version gets its own file, so old versions stay reviewable
    rather than being silently clobbered."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{capability.capability_id}__v{capability.version}.json"
    path.write_text(capability.model_dump_json(indent=2))
    return path


def load_capability(path: Path | str) -> Capability:
    return Capability.model_validate_json(Path(path).read_text())

"""Render a capability as prose a non-engineer can check.

Section 3.2 asks for an artifact that is reviewable by "both a human reviewer
and a calling agent". The JSON serves the agent well and the reviewer badly:
approving a piece of banking automation should not require reading nested
`ControlRef` objects to work out that step 4 clicks an unlabelled icon.

So the same artifact is also written as a playbook. It is GENERATED, never
edited — a hand-maintained description drifts from the thing it describes,
and a stale playbook is worse than none because it is still believed.

Two things it deliberately states plainly rather than glossing:

  - How each control is identified, including when the automation is relying
    on position rather than a name. A reviewer who cannot see that "the
    button after the Member ID label" is positional cannot judge how fragile
    it is.
  - Which single step actually commits. Everything before it can be
    abandoned harmlessly; that one has consequences, and it is where
    approval is requested.
"""

from __future__ import annotations

from computer_use.artifact import Capability, CapabilityStep
from computer_use.contracts import ControlRef, Verb

def _plain_value(value: str | None) -> str:
    """Bindings read as English; literals read as themselves."""
    if not value:
        return ""
    if value.startswith("${input.") and value.endswith("}"):
        return f"the supplied `{value[8:-1]}`"
    return f"`{value}`"


def _describe_target(target: ControlRef | None) -> str:
    """Say how the control is found, in the order the driver tries it."""
    if target is None:
        return "the page itself"

    ways: list[str] = []
    semantic = target.semantic
    if semantic is not None:
        if semantic.row_anchor is not None:
            ways.append(
                f"the **{semantic.column}** cell of the row whose "
                f"**{semantic.row_anchor.column}** is "
                f"{_plain_value(semantic.row_anchor.equals)}"
            )
        elif semantic.name:
            match = "containing" if semantic.match == "contains" else "named"
            ways.append(f"the {semantic.role or 'control'} {match} **{semantic.name}**")
        elif semantic.role:
            ways.append(f"the {semantic.role} on the page")

    structural = target.structural
    if structural is not None and structural.anchor is not None:
        direction, role = (
            structural.path.split(":", 1) if ":" in structural.path else ("following", "control")
        )
        where = "after" if direction == "following" else "before"
        ways.append(
            f"failing that, the first {role} {where} the text "
            f"**{structural.anchor.name}** (found by position, not by name)"
        )

    return "; ".join(ways) if ways else "an unspecified control"


def _describe_step(step: CapabilityStep) -> str:
    """One step as a sentence.

    Built per-verb rather than by slotting a value onto a generic template:
    "Type X into Y" and "Read Y, keep it as X" put the value in different
    places, and a template that ignores that produces the value stranded at
    the end of the sentence, after the parenthetical explaining how the
    control is found. Which is where it landed in the first version.
    """
    target = _describe_target(step.action.target)
    value = _plain_value(step.action.value)

    match step.action.verb:
        case Verb.CLICK:
            return f"**Click** {target}."
        case Verb.TYPE:
            return f"**Type** {value} into {target}."
        case Verb.SELECT:
            return f"**Choose** {value} in {target}."
        case Verb.READ:
            sentence = f"**Read** {target}."
            if step.action.output_key:
                sentence += f" Keep it as `{step.action.output_key}`."
            return sentence
        case Verb.NAVIGATE:
            return f"**Go to** {value}."
    return f"**{step.action.verb.value.title()}** {target}."


def render(capability: Capability) -> str:
    """Produce the playbook for one capability."""
    lines: list[str] = [
        f"# {capability.capability_id}",
        "",
        f"*Version {capability.version} · recorded against {capability.target_surface}*",
        "",
        "> Generated from the capability artifact. Do not edit — regenerate it. A",
        "> hand-maintained description drifts from what actually runs, and a stale",
        "> playbook is worse than none, because it is still believed.",
        "",
        capability.description,
        "",
        "## What it needs",
        "",
    ]

    if capability.inputs:
        lines.append("| Input | Type | Required |")
        lines.append("|---|---|---|")
        for param in capability.inputs:
            lines.append(
                f"| `{param.name}` | {param.type} | {'yes' if param.required else 'no'} |"
            )
    else:
        lines.append("Nothing — it takes no parameters.")
    lines.append("")

    lines += ["## What it gives back", ""]
    if capability.outputs:
        lines.append("| Output | Type |")
        lines.append("|---|---|")
        for field in capability.outputs:
            lines.append(f"| `{field.name}` | {field.type} |")
    else:
        lines.append("Nothing — it performs an action but returns no data.")
    lines.append("")

    lines += [
        "## What it does, step by step",
        "",
        f"Starting at `{capability.entry_path}`, signed in as whoever the caller",
        "authenticated. Signing in is not part of this capability — credentials are",
        "never recorded in an artifact.",
        "",
    ]

    for index, step in enumerate(capability.steps, start=1):
        lines.append(f"{index}. {_describe_step(step)}")

        if step.step_id == capability.commit_step_id:
            lines.append("")
            lines.append(
                "   > ⚠️ **This is the step that commits.** Everything above it can be "
                "abandoned with no effect; this one has consequences. Policy is checked "
                "immediately before it, and a high-value action is held here for a "
                "person to authorise."
            )
        lines.append("")

    lines += [
        "## How it knows it worked",
        "",
        f"It checks the screen for {_describe_target(capability.checkpoint)}. If that",
        "is not there, the run is reported as a failure rather than a success — a",
        "click is not evidence that the click did anything.",
        "",
        "## If something goes wrong",
        "",
        "- A legitimate answer — no such member, a closed account, insufficient funds,",
        "  an amount over the limit — comes back as a **business outcome** with a code,",
        "  not an error. Nothing is broken; that is the answer.",
        "- A control that cannot be found, or that matches more than one thing, is a",
        "  **failure**. The run stops rather than guessing which one to click.",
        "- If the session expires part-way, it re-authenticates. Whether it may then",
        f"  re-run from the start is declared by this capability: "
        f"**{'yes' if capability.restartable_after_recovery else 'no'}**"
        + (
            "."
            if capability.restartable_after_recovery
            else " — re-running could repeat an action that already went through."
        ),
        "",
    ]
    return "\n".join(lines)


def write_playbook(capability: Capability, directory: str = "capabilities") -> str:
    """Write the playbook next to the artifact it describes."""
    from pathlib import Path

    path = Path(directory) / f"{capability.capability_id}__v{capability.version}.md"
    path.write_text(render(capability))
    return str(path)

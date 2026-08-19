"""Hand-author the transfer capability artifact.

Written by hand rather than discovered, for two reasons. First, it keeps the
policy demo free: no API call is needed to show a guardrail working. Second,
it makes a point the schema is supposed to support — a Capability is a
reviewable document, not an opaque model output. A human can write one, and
more importantly a human can READ one and correct it, which is the whole
argument for a typed artifact rather than a saved transcript.

Usage:
    .venv/bin/python scripts/make_transfer_capability.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from computer_use.artifact import (
    Capability,
    CapabilityStep,
    InputParam,
    OutputField,
    save_capability,
)
from computer_use.contracts import (
    Action,
    ControlRef,
    LocatorTier,
    RowAnchor,
    SemanticRef,
    StructuralRef,
    Verb,
)


def near(text: str, role: str) -> StructuralRef:
    """The <role> next to <text> — the same disambiguation the discovery
    agent reaches for when a name alone matches more than one control."""
    return StructuralRef(
        anchor=SemanticRef(role="text", name=text, match="contains"),
        path=f"following:{role}",
    )


capability = Capability(
    capability_id="transfer_between_accounts",
    version="1.0.0",
    description=(
        "Move funds between two of a member's own accounts and reach the "
        "posted-transfer confirmation."
    ),
    target_surface="http://127.0.0.1:5000",
    entry_path="/",
    inputs=(
        InputParam(name="member_id", type="string", description="Member to act on."),
        InputParam(name="from_account", type="string", description="Source account number."),
        InputParam(name="to_account", type="string", description="Destination account number."),
        InputParam(name="amount", type="number", description="Amount to move, in dollars."),
    ),
    outputs=(
        OutputField(
            name="source_balance_before",
            type="string",
            description="Source account balance read before the transfer is posted.",
        ),
    ),
    steps=(
        CapabilityStep(
            step_id="step_1",
            intent="Enter the member id in the Member ID search box",
            recorded_tier=LocatorTier.STRUCTURAL,
            action=Action(
                verb=Verb.TYPE,
                target=ControlRef(
                    semantic=SemanticRef(role="textbox"),
                    structural=near("Member ID:", "textbox"),
                ),
                value="${input.member_id}",
                intent="Enter the member id in the Member ID search box",
            ),
        ),
        CapabilityStep(
            step_id="step_2",
            intent="Search for the member",
            recorded_tier=LocatorTier.STRUCTURAL,
            action=Action(
                verb=Verb.CLICK,
                target=ControlRef(
                    semantic=SemanticRef(role="button", name="Search"),
                    structural=near("Member ID:", "button"),
                ),
                intent="Search for the member",
            ),
        ),
        CapabilityStep(
            step_id="step_3",
            intent="Open the member's detail page",
            recorded_tier=LocatorTier.SEMANTIC,
            action=Action(
                verb=Verb.CLICK,
                target=ControlRef(semantic=SemanticRef(role="link", name="View")),
                intent="Open the member's detail page",
            ),
        ),
        CapabilityStep(
            step_id="step_4",
            intent="Read the source account balance before moving anything",
            recorded_tier=LocatorTier.SEMANTIC,
            action=Action(
                verb=Verb.READ,
                target=ControlRef(
                    semantic=SemanticRef(
                        row_anchor=RowAnchor(column="Number", equals="${input.from_account}"),
                        column="Balance",
                    )
                ),
                output_key="source_balance_before",
                intent="Read the source account balance before moving anything",
            ),
        ),
        CapabilityStep(
            step_id="step_5",
            intent="Open the transfer screen",
            recorded_tier=LocatorTier.SEMANTIC,
            action=Action(
                verb=Verb.CLICK,
                target=ControlRef(semantic=SemanticRef(role="link", name="Transfer Funds")),
                intent="Open the transfer screen",
            ),
        ),
        CapabilityStep(
            step_id="step_6",
            intent="Choose the source account",
            recorded_tier=LocatorTier.STRUCTURAL,
            action=Action(
                verb=Verb.SELECT,
                target=ControlRef(
                    semantic=SemanticRef(role="combobox"),
                    structural=near("From Account:", "combobox"),
                ),
                value="${input.from_account}",
                intent="Choose the source account",
            ),
        ),
        CapabilityStep(
            step_id="step_7",
            intent="Choose the destination account",
            recorded_tier=LocatorTier.STRUCTURAL,
            action=Action(
                verb=Verb.SELECT,
                target=ControlRef(
                    semantic=SemanticRef(role="combobox"),
                    structural=near("To Account:", "combobox"),
                ),
                value="${input.to_account}",
                intent="Choose the destination account",
            ),
        ),
        CapabilityStep(
            step_id="step_8",
            intent="Enter the transfer amount",
            recorded_tier=LocatorTier.STRUCTURAL,
            action=Action(
                verb=Verb.TYPE,
                target=ControlRef(
                    semantic=SemanticRef(role="textbox"),
                    structural=near("Transfer Amount", "textbox"),
                ),
                value="${input.amount}",
                intent="Enter the transfer amount",
            ),
        ),
        CapabilityStep(
            step_id="step_9",
            intent="Post the transfer",
            recorded_tier=LocatorTier.SEMANTIC,
            action=Action(
                verb=Verb.CLICK,
                target=ControlRef(semantic=SemanticRef(role="button", name="Post Transfer")),
                intent="Post the transfer",
            ),
        ),
    ),
    checkpoint=ControlRef(
        semantic=SemanticRef(role="text", name="Transfer posted successfully", match="contains")
    ),
    # The click that actually moves money. Policy asks for authorisation
    # immediately before this step and nowhere else.
    commit_step_id="step_9",
    # Emphatically not restartable: re-running a flow that may already have
    # posted would post it twice.
    restartable_after_recovery=False,
)


if __name__ == "__main__":
    path = save_capability(capability)
    print(f"Wrote {path}")
    print(f"  steps:       {len(capability.steps)}")
    print(f"  commit step: {capability.commit_step_id}")
    print(f"  inputs:      {[i.name for i in capability.inputs]}")

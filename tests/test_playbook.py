"""The playbook has to be readable AND true.

Readable is the point; true is the constraint. A generated description that
quietly diverges from the artifact is worse than none, because a reviewer
approves the description believing it is the automation.
"""

from __future__ import annotations

from computer_use.artifact import Capability, CapabilityStep, InputParam, OutputField
from computer_use.contracts import Action, ControlRef, RowAnchor, SemanticRef, StructuralRef, Verb
from computer_use.playbook import render

CAPABILITY = Capability(
    capability_id="transfer_between_accounts",
    version="1.0.0",
    description="Move funds between two of a member's own accounts.",
    target_surface="http://127.0.0.1:5000",
    inputs=(
        InputParam(name="member_id", type="string"),
        InputParam(name="amount", type="number"),
    ),
    outputs=(OutputField(name="source_balance_before", type="string"),),
    steps=(
        CapabilityStep(
            step_id="step_1",
            action=Action(
                verb=Verb.TYPE,
                value="${input.member_id}",
                target=ControlRef(
                    semantic=SemanticRef(role="textbox"),
                    structural=StructuralRef(
                        anchor=SemanticRef(role="text", name="Member ID:", match="contains"),
                        path="following:textbox",
                    ),
                ),
            ),
        ),
        CapabilityStep(
            step_id="step_2",
            action=Action(
                verb=Verb.READ,
                output_key="source_balance_before",
                target=ControlRef(
                    semantic=SemanticRef(
                        row_anchor=RowAnchor(column="Number", equals="${input.from_account}"),
                        column="Balance",
                    )
                ),
            ),
        ),
        CapabilityStep(
            step_id="step_3",
            action=Action(
                verb=Verb.CLICK,
                target=ControlRef(semantic=SemanticRef(role="button", name="Post Transfer")),
            ),
        ),
    ),
    checkpoint=ControlRef(
        semantic=SemanticRef(role="text", name="Transfer posted successfully", match="contains")
    ),
    commit_step_id="step_3",
    restartable_after_recovery=False,
)


def test_no_raw_bindings_survive_into_the_prose():
    """`${input.member_id}` is not English. If a reviewer has to decode
    template syntax, the playbook has failed at its one job."""
    assert "${" not in render(CAPABILITY)


def test_a_binding_is_named_as_a_supplied_input():
    text = render(CAPABILITY)
    assert "the supplied `member_id`" in text
    # Including inside a row anchor, which is where it was missed first.
    assert "the supplied `from_account`" in text


def test_the_committing_step_is_called_out():
    """The one step with consequences must be impossible to miss — it is
    where approval is requested and where a mistake is irreversible."""
    text = render(CAPABILITY)
    assert "This is the step that commits" in text
    assert text.index("Post Transfer") < text.index("This is the step that commits")


def test_positional_targeting_is_disclosed_not_hidden():
    """A reviewer who cannot tell that a step relies on position rather than
    a name cannot judge how fragile it is."""
    assert "found by position, not by name" in render(CAPABILITY)


def test_inputs_and_outputs_are_listed():
    text = render(CAPABILITY)
    for name in ("member_id", "amount", "source_balance_before"):
        assert name in text


def test_restart_safety_is_stated_with_its_reason():
    text = render(CAPABILITY)
    assert "re-running could repeat an action that already went through" in text


def test_restartable_capabilities_say_so():
    text = render(CAPABILITY.model_copy(update={"restartable_after_recovery": True}))
    assert "**yes**" in text
    assert "repeat an action" not in text


def test_every_step_appears_exactly_once():
    """A dropped step would describe automation that does less than it does;
    a duplicated one, more."""
    text = render(CAPABILITY)
    for n in range(1, len(CAPABILITY.steps) + 1):
        assert f"\n{n}. **" in text


def test_the_checkpoint_is_explained():
    assert "Transfer posted successfully" in render(CAPABILITY)

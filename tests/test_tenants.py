"""Cross-tenant specialization.

The claim under test: one recorded capability serves many institutions
running the same product, with each institution carrying only its
differences — and the base is never mutated in the process, since one loaded
base is specialized for many tenants in the same process.
"""

from __future__ import annotations

from computer_use.artifact import Capability, CapabilityStep, InputParam, OutputField
from computer_use.contracts import (
    Action,
    ControlRef,
    RowAnchor,
    SemanticRef,
    StructuralRef,
    Verb,
)
from computer_use.tenants import TenantProfile, specialize

BASE = Capability(
    capability_id="c",
    version="1.0.0",
    description="d",
    target_surface="http://base",
    inputs=(InputParam(name="member_id"),),
    outputs=(OutputField(name="balance"),),
    steps=(
        CapabilityStep(
            step_id="step_1",
            action=Action(
                verb=Verb.CLICK,
                target=ControlRef(
                    semantic=SemanticRef(role="button", name="Search"),
                    structural=StructuralRef(
                        anchor=SemanticRef(role="text", name="Account Type", match="contains"),
                        path="following:button",
                    ),
                ),
            ),
        ),
        CapabilityStep(
            step_id="step_2",
            action=Action(
                verb=Verb.READ,
                output_key="balance",
                target=ControlRef(
                    semantic=SemanticRef(
                        row_anchor=RowAnchor(column="Account Type", equals="Savings"),
                        column="Balance",
                    )
                ),
            ),
        ),
    ),
    checkpoint=ControlRef(semantic=SemanticRef(role="text", name="Search", match="contains")),
)

TENANT = TenantProfile(
    tenant_id="t2",
    target_surface="http://tenant2",
    label_map={"Search": "Find", "Account Type": "Product"},
)


def test_renames_apply_to_semantic_names():
    out = specialize(BASE, TENANT)
    assert out.steps[0].action.target.semantic.name == "Find"


def test_renames_apply_to_structural_anchor_text():
    """A rename shows up in the anchor too — the structural tier navigates
    relative to on-screen text, which is exactly what got renamed."""
    out = specialize(BASE, TENANT)
    assert out.steps[0].action.target.structural.anchor.name == "Product"


def test_renames_apply_to_row_anchor_column_headers():
    """A renamed COLUMN breaks a row anchor as surely as a renamed button
    breaks a click, because the anchor matches on header text."""
    out = specialize(BASE, TENANT)
    anchor = out.steps[1].action.target.semantic.row_anchor
    assert anchor.column == "Product"
    # The VALUE in the row is member data, not a label — it must not be
    # rewritten by a label map.
    assert anchor.equals == "Savings"


def test_checkpoint_is_specialized_too():
    out = specialize(BASE, TENANT)
    assert out.checkpoint.semantic.name == "Find"


def test_base_is_not_mutated():
    """One loaded base, many tenants, same process."""
    specialize(BASE, TENANT)
    assert BASE.steps[0].action.target.semantic.name == "Search"
    assert BASE.target_surface == "http://base"


def test_specialized_capability_is_traceable_to_its_tenant():
    out = specialize(BASE, TENANT)
    assert out.version == "1.0.0+t2"
    assert out.target_surface == "http://tenant2"


def test_unmapped_labels_are_left_alone():
    out = specialize(BASE, TENANT)
    assert out.steps[1].action.target.semantic.column == "Balance"


def test_step_override_wins_over_label_map():
    """The escape hatch: a control that MOVED rather than got renamed."""
    tenant = TENANT.model_copy(
        update={
            "step_overrides": {
                "step_1": ControlRef(semantic=SemanticRef(role="link", name="Go"))
            }
        }
    )
    out = specialize(BASE, tenant)
    assert out.steps[0].action.target.semantic.role == "link"
    assert out.steps[0].action.target.semantic.name == "Go"

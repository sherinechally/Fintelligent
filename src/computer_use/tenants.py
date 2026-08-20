"""Reusing one recorded capability across institutions running the same product.

Hundreds of institutions run the same vendor software, configured and
branded differently: one calls a control "Search", the next calls it "Find
Member". The flow is identical; the words are not. Re-recording per tenant
would mean 300 recordings of the same flow and 300 re-fixes every time the
vendor ships an update.

So a capability is recorded ONCE against a base install, and each tenant
carries only its differences.

Those differences are expressed as a LABEL MAP rather than per-step
patches:

    {"Search": "Find Member", "Account Type": "Product"}

Per-step patches ("step_2: use this locator instead") tie the override to
step NUMBERS, which change the moment the base capability is re-recorded —
so every tenant's overrides silently rot on the next recording. A label map
is stated in terms of the base's own vocabulary, so it survives
re-recording, applies wherever that label appears rather than once per
occurrence, and can be read and corrected by whoever administers the tenant
rather than only by whoever wrote the automation.

`step_overrides` remains as an escape hatch for the case a label map cannot
express — a control that genuinely moved rather than merely got renamed.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from computer_use.artifact import Capability, CapabilityStep
from computer_use.contracts import ControlRef, RowAnchor, SemanticRef, StructuralRef


class TenantProfile(BaseModel):
    """One institution's deviations from the base install."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    display_name: str = ""
    target_surface: str
    """Where this tenant's install lives. The only field every tenant must
    set — even a tenant that changed nothing still runs somewhere else."""

    label_map: dict[str, str] = Field(default_factory=dict)
    """Base label -> this tenant's label. Applied to accessible names,
    structural anchor text, and row-anchor column headers, because a rename
    shows up in all three."""

    step_overrides: dict[str, ControlRef] = Field(default_factory=dict)
    """Escape hatch: step_id -> a replacement ControlRef, for drift a label
    map cannot describe. Tied to step ids and therefore fragile by nature —
    if this dict is filling up, the base capability probably needs
    re-recording for this tenant rather than patching."""

    def relabel(self, text: str | None) -> str | None:
        return self.label_map.get(text, text) if text is not None else None


def _specialize_ref(ref: ControlRef | None, tenant: TenantProfile) -> ControlRef | None:
    if ref is None:
        return None

    semantic = ref.semantic
    if semantic is not None:
        anchor = semantic.row_anchor
        if anchor is not None:
            anchor = RowAnchor(
                # A renamed COLUMN HEADER matters as much as a renamed
                # button: "the Balance cell in the row whose Account Type is
                # Savings" is two labels, and a tenant may have changed
                # either.
                column=tenant.relabel(anchor.column) or anchor.column,
                equals=anchor.equals,
            )
        semantic = SemanticRef(
            role=semantic.role,
            name=tenant.relabel(semantic.name),
            match=semantic.match,
            row_anchor=anchor,
            column=tenant.relabel(semantic.column),
        )

    structural = ref.structural
    if structural is not None and structural.anchor is not None:
        structural = StructuralRef(
            anchor=SemanticRef(
                role=structural.anchor.role,
                name=tenant.relabel(structural.anchor.name),
                match=structural.anchor.match,
            ),
            path=structural.path,
        )

    return ControlRef(semantic=semantic, structural=structural, visual=ref.visual)


def specialize(capability: Capability, tenant: TenantProfile) -> Capability:
    """Produce this tenant's view of a base capability.

    Returns a new Capability; the base is never mutated, so one loaded base
    can be specialized for many tenants in the same process. The result
    carries the tenant's surface and a version suffix, so evidence from a
    tenant run says which variant it was.
    """
    steps = []
    for step in capability.steps:
        override = tenant.step_overrides.get(step.step_id)
        target = override if override is not None else _specialize_ref(step.action.target, tenant)
        steps.append(
            CapabilityStep(
                step_id=step.step_id,
                action=step.action.model_copy(update={"target": target}),
                intent=step.intent,
                recorded_tier=step.recorded_tier,
            )
        )

    return capability.model_copy(
        update={
            "version": f"{capability.version}+{tenant.tenant_id}",
            "target_surface": tenant.target_surface,
            "steps": tuple(steps),
            "checkpoint": _specialize_ref(capability.checkpoint, tenant),
        }
    )


def load_tenant(path: Path | str) -> TenantProfile:
    return TenantProfile.model_validate_json(Path(path).read_text())


def save_tenant(tenant: TenantProfile, directory: Path | str = "tenants") -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tenant.tenant_id}.json"
    path.write_text(tenant.model_dump_json(indent=2, exclude_defaults=True))
    return path

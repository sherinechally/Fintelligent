"""Run ONE recorded capability against TWO institutions.

Both run the same vendor product. Harborlight has relabelled it — "Search"
is "Find", "View" is "Open", the Account Type column is "Product", the
sub-account link is "Add Sub-Account". Nothing about the FLOW differs.

The demo runs the base artifact three ways:

  1. against Northbay (where it was recorded)          -> works
  2. against Harborlight with NO overrides             -> fails, and says
                                                          exactly which
                                                          control it could
                                                          not find
  3. against Harborlight WITH its label map            -> works

Run 2 matters as much as run 3: it shows the failure is diagnosable rather
than mysterious, and it shows which steps survived the rename on their own —
those anchored to labels the tenant did not change resolve unaided, because
the structural tier is positional relative to text that is still there.

Usage:
    TENANT=northbay    PORT=5000 .venv/bin/python target_app/app.py &
    TENANT=harborlight PORT=5001 .venv/bin/python target_app/app.py &
    .venv/bin/python scripts/run_cross_tenant_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from computer_use.artifact import load_capability
from computer_use.contracts import BusinessOutcome, Failure, Success
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.replay.engine import ReplayEngine
from computer_use.tenants import TenantProfile, save_tenant, specialize

ARTIFACT = Path("capabilities/read_balance_and_open_subaccount__v1.0.0.json")

NORTHBAY = TenantProfile(
    tenant_id="northbay",
    display_name="Northbay Credit Union",
    target_surface=os.environ.get("NORTHBAY_URL", "http://127.0.0.1:5000"),
    # No label map: this is where the capability was recorded, so by
    # definition it has nothing to override.
)

HARBORLIGHT = TenantProfile(
    tenant_id="harborlight",
    display_name="Harborlight Federal",
    target_surface=os.environ.get("HARBORLIGHT_URL", "http://127.0.0.1:5001"),
    # The entire cost of onboarding this institution. Stated in the BASE
    # install's vocabulary, so it survives the base being re-recorded.
    label_map={
        "Search": "Find",
        "View": "Open",
        "Account Type": "Product",
        "Open New Sub-Account": "Add Sub-Account",
    },
)

INPUTS = {
    "member_id": "10234",
    "filter_account_type": "Savings",
    "account_type": "Money Market",
    "initial_deposit": "500",
}


def replay_against(capability, base_url: str):
    operator = os.environ.get("TARGET_APP_OPERATOR", "s.patel")
    password = os.environ.get("TARGET_APP_PASSWORD", "demo")
    driver = PlaywrightDriver(base_url)
    driver.start(headless=True)
    try:
        driver.goto("/login")
        driver._page.fill('input[name="username"]', operator)
        driver._page.fill('input[name="password"]', password)
        driver._page.click('input[type="submit"]')
        engine = ReplayEngine(driver, credentials=(operator, password))
        return engine.run(capability, INPUTS), engine
    finally:
        driver.close()


def describe(result) -> str:
    match result:
        case Success():
            return f"SUCCESS  outputs={result.outputs}"
        case BusinessOutcome():
            return f"BUSINESS OUTCOME  {result.code}"
        case Failure():
            return f"FAILURE  {result.failure_class} at {result.step_id or result.phase}"
    return "?"


def main() -> None:
    base = load_capability(ARTIFACT)
    print(f"Base capability: {base.capability_id} v{base.version}")
    print(f"Recorded against: {base.target_surface}\n")

    print("=" * 74)
    print("  1. Northbay — the install it was recorded against")
    print("=" * 74)
    result, engine = replay_against(specialize(base, NORTHBAY), NORTHBAY.target_surface)
    print(f"  {describe(result)}\n")

    print("=" * 74)
    print("  2. Harborlight — SAME artifact, NO overrides")
    print("=" * 74)
    naive = base.model_copy(update={"target_surface": HARBORLIGHT.target_surface})
    result_naive, engine_naive = replay_against(naive, HARBORLIGHT.target_surface)
    print(f"  {describe(result_naive)}")
    if isinstance(result_naive, Failure):
        print(f"  expected: {result_naive.expected}")
        print(f"  observed: {result_naive.observed}")
    survived = [s.step_id for s in engine_naive.steps if s.ok]
    print(f"\n  Steps that resolved anyway, despite the rename: {survived}")
    print("  (those are anchored to labels Harborlight did not change — the")
    print("   structural tier is positional relative to text that is still there)\n")

    print("=" * 74)
    print("  3. Harborlight — SAME artifact + its label map")
    print("=" * 74)
    for base_label, tenant_label in HARBORLIGHT.label_map.items():
        print(f'    "{base_label}" -> "{tenant_label}"')
    print()
    result_tenant, _ = replay_against(
        specialize(base, HARBORLIGHT), HARBORLIGHT.target_surface
    )
    print(f"  {describe(result_tenant)}\n")

    path = save_tenant(HARBORLIGHT)
    print("=" * 74)
    print(f"  One recording. Two institutions. {len(HARBORLIGHT.label_map)} lines of config.")
    print(f"  Tenant profile written to {path}")
    print("=" * 74)


if __name__ == "__main__":
    main()

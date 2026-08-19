"""Demonstrate human-in-the-loop escalation on a live replay.

Uses the one condition our own artifact genuinely cannot resolve: searching
"1" matches three members, so the recorded "View" link — which was
unambiguous when recorded, because that search returned a single row —
now matches three. Replay refuses to guess (clicking the wrong member's
account is exactly the mistake worth stopping for) and hands you the
browser.

You are then driving THE SAME live session: same window, same cookies, same
page. Click the member you want, then return here and choose `resume`.
Replay re-checks the blocked step and continues from there.

Usage:
    .venv/bin/python target_app/app.py &
    .venv/bin/python scripts/run_handoff_demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from computer_use.artifact import load_capability
from computer_use.contracts import BusinessOutcome, Failure, Holder, Success
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.escalation.handoff import CliOperatorConsole, HandoffCoordinator
from computer_use.replay.engine import ReplayEngine

BASE_URL = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5000")
ARTIFACT = Path("capabilities/read_balance_and_open_subaccount__v1.0.0.json")


def main() -> None:
    operator = os.environ.get("TARGET_APP_OPERATOR", "s.patel")
    password = os.environ.get("TARGET_APP_PASSWORD", "demo")

    capability = load_capability(ARTIFACT)
    # "1" matches all three demo members -> three identical "View" links.
    inputs = {
        "member_id": "1",
        "filter_account_type": "Savings",
        "account_type": "Money Market",
        "initial_deposit": "500",
    }

    print(f"Capability: {capability.capability_id} v{capability.version}")
    print(f"Inputs:     {inputs}   <- deliberately ambiguous")
    print("Escalation: enabled (a human is available)\n")

    driver = PlaywrightDriver(BASE_URL)
    driver.start(headless=False, slow_mo=400)  # must be headed: you drive it
    try:
        driver.goto("/login")
        driver._page.fill('input[name="username"]', operator)
        driver._page.fill('input[name="password"]', password)
        driver._page.click('input[type="submit"]')

        engine = ReplayEngine(
            driver,
            credentials=(operator, password),
            handoff=HandoffCoordinator(driver, CliOperatorConsole()),
        )
        result = engine.run(capability, inputs)

        print()
        print("=" * 72)
        print("  TIMELINE (one log, both actors)")
        print("=" * 72)
        for step in engine.steps:
            who = "HUMAN " if step.actor is Holder.HUMAN else "auto  "
            mark = "OK  " if step.ok else "FAIL"
            label = step.step_id or "-"
            print(f"  [{mark}] {who} {label:8s} {step.intent or step.note}")
            if step.intent and step.note:
                print(f"                       note: {step.note}")

        print()
        print("=" * 72)
        match result:
            case Success():
                print("  RESULT: SUCCESS (completed after the handoff)")
                print(f"    outputs:  {result.outputs}")
            case BusinessOutcome():
                print("  RESULT: BUSINESS OUTCOME")
                print(f"    {result.code}: {result.message}")
            case Failure():
                print("  RESULT: FAILURE")
                print(f"    class:    {result.failure_class}")
                print(f"    phase:    {result.phase}")
                print(f"    observed: {result.observed}")
        print("=" * 72)

        input("\nPress Enter to close the browser... ")
    finally:
        driver.close()


if __name__ == "__main__":
    main()

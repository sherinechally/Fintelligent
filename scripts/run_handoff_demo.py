"""Demonstrate human-in-the-loop escalation on a high-value transfer.

The escalation here is a JUDGMENT call, not a technical hiccup. The
capability moves money between a member's accounts. The target application
imposes no limit at all — type any number and it posts. Our policy layer
does: above $10,000 the action may not run unattended, and above $100,000 it
may not run at all.

So replay walks the flow, reads the source balance, and then stops on the
very step that would move the money — with the amount, the limit, the risk
class, and the balance it just read, all in front of the person deciding.
That is a call a human can actually make; "three rows matched a locator" is
not.

You hold the SAME live browser session while you decide. The automation
cannot act on it until you hand control back.

Usage:
    .venv/bin/python target_app/app.py &
    .venv/bin/python scripts/run_handoff_demo.py                # $11,000 -> needs you
    .venv/bin/python scripts/run_handoff_demo.py --amount 500   # routine, no stop
    .venv/bin/python scripts/run_handoff_demo.py --amount 250000  # hard ceiling
"""

from __future__ import annotations

import argparse
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
ARTIFACT = Path("capabilities/transfer_between_accounts__v1.0.0.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--member-id", default="10234")
    parser.add_argument("--from-account", default="...4471")
    parser.add_argument("--to-account", default="...1029")
    parser.add_argument(
        "--amount",
        default="11000",
        help="under 10000 runs unattended; over 10000 needs you; over 100000 is refused outright",
    )
    parser.add_argument("--slow", type=int, default=400)
    args = parser.parse_args()

    operator = os.environ.get("TARGET_APP_OPERATOR", "s.patel")
    password = os.environ.get("TARGET_APP_PASSWORD", "demo")

    capability = load_capability(ARTIFACT)
    inputs = {
        "member_id": args.member_id,
        "from_account": args.from_account,
        "to_account": args.to_account,
        "amount": args.amount,
    }

    print(f"Capability: {capability.capability_id} v{capability.version}")
    print(f"Inputs:     {inputs}")
    print(f"Commit step: {capability.commit_step_id} (policy is consulted immediately before it)")
    print("The target app enforces NO limit on this. Our policy layer does.\n")

    driver = PlaywrightDriver(BASE_URL)
    driver.start(headless=False, slow_mo=args.slow)
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
            mark = "OK  " if step.ok else "STOP"
            print(f"  [{mark}] {who} {(step.step_id or '-'):8s} {step.intent or step.note}")
            if step.intent and step.note:
                print(f"                        note: {step.note}")

        print()
        print("=" * 72)
        match result:
            case Success():
                print("  RESULT: SUCCESS — the transfer was posted.")
                print(f"    outputs: {result.outputs}")
            case BusinessOutcome():
                print("  RESULT: BUSINESS OUTCOME (a real answer, not an error)")
                print(f"    {result.code}: {result.message}")
            case Failure():
                print("  RESULT: FAILURE — nothing was posted.")
                print(f"    class:    {result.failure_class}")
                print(f"    expected: {result.expected}")
                print(f"    observed: {result.observed}")
        print("=" * 72)

        input("\nPress Enter to close the browser... ")
    finally:
        driver.close()


if __name__ == "__main__":
    main()

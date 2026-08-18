"""Replay a saved capability deterministically — NO LLM involved.

This is the production path: what an AI agent would trigger when it wants
this capability executed. No ANTHROPIC_API_KEY is needed or used.

Usage:
    .venv/bin/python target_app/app.py &
    .venv/bin/python scripts/run_replay.py                      # happy path
    .venv/bin/python scripts/run_replay.py --member-id 99999    # not found
    .venv/bin/python scripts/run_replay.py --member-id 11002    # permission denied
    .venv/bin/python scripts/run_replay.py --deposit 5          # below minimum
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from computer_use.artifact import load_capability
from computer_use.contracts import BusinessOutcome, Failure, Success
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.replay.engine import ReplayEngine

BASE_URL = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5000")
ARTIFACT = Path("capabilities/read_balance_and_open_subaccount__v1.0.0.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--member-id", default="10234")
    parser.add_argument("--filter-account-type", default="Savings")
    parser.add_argument("--account-type", default="Money Market")
    parser.add_argument("--deposit", default="500")
    parser.add_argument(
        "--headed", action="store_true", help="show the browser window while replaying"
    )
    parser.add_argument(
        "--operator",
        default=os.environ.get("TARGET_APP_OPERATOR", "s.patel"),
        help="which operator to sign in as. s.patel = Officer (entitled), "
        "t.nguyen = Teller (view only, will hit NOT_ENTITLED).",
    )
    parser.add_argument(
        "--expire-session-after",
        type=int,
        default=0,
        metavar="N",
        help="force the session to expire after N steps, to demonstrate "
        "recovery from a mid-flow session expiry.",
    )
    parser.add_argument(
        "--slow",
        type=int,
        default=0,
        metavar="MS",
        help="pause this many ms between actions so a headed run is watchable "
        "(replay normally finishes in well under a second). Try --slow 700.",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="close the browser immediately instead of holding it open at the "
        "final screen until you press Enter (headed runs only)",
    )
    args = parser.parse_args()

    capability = load_capability(ARTIFACT)
    inputs = {
        "member_id": args.member_id,
        "filter_account_type": args.filter_account_type,
        "account_type": args.account_type,
        "initial_deposit": args.deposit,
    }

    # Demo credentials for the local mock only. In a real deployment these
    # would come from a secret store or a Kerberos keytab — never a literal,
    # never the artifact, never the evidence log.
    password = os.environ.get("TARGET_APP_PASSWORD", "demo")

    print(f"Capability: {capability.capability_id} v{capability.version}")
    print(f"Inputs:     {inputs}")
    print(f"Operator:   {args.operator}")
    print("LLM:        not used (deterministic replay)\n")

    driver = PlaywrightDriver(BASE_URL)
    driver.start(headless=not args.headed, slow_mo=args.slow)
    try:
        # Sign in before replaying — the capability's steps start from an
        # already-authenticated session.
        driver.goto("/login")
        page = driver._page
        page.fill('input[name="username"]', args.operator)
        page.fill('input[name="password"]', password)
        page.click('input[type="submit"]')

        after_step = None
        if args.expire_session_after:
            fired: list[bool] = []

            def after_step(seq: int, _n=args.expire_session_after) -> None:
                # ONE-SHOT: expiring again on the restarted run would just
                # exhaust recovery and prove nothing about resuming.
                if seq == _n and not fired:
                    fired.append(True)
                    print(f"  [demo] forcing session expiry after step {seq}")
                    driver._page.goto(f"{BASE_URL}/expire-session")

        engine = ReplayEngine(
            driver, credentials=(args.operator, password), after_step=after_step
        )
        result = engine.run(capability, inputs)

        for step in engine.steps:
            marker = "OK  " if step.ok else "FAIL"
            print(f"[{marker}] {step.step_id:8s} | {step.intent}")
            if step.note:
                print(f"          note: {step.note}")

        print()
        print("=" * 70)
        match result:
            case Success():
                print("RESULT: SUCCESS")
                print(f"  outputs:  {result.outputs}")
                print(f"  duration: {result.duration_ms} ms")
                if result.drifted_steps:
                    print(
                        "  DRIFT — these steps no longer resolve the way they were "
                        f"recorded: {result.drifted_steps}"
                    )
            case BusinessOutcome():
                print("RESULT: BUSINESS OUTCOME (not an error — a real answer)")
                print(f"  code:     {result.code}")
                print(f"  message:  {result.message}")
                print(f"  at step:  {result.detected_at_step}")
                if result.partial_outputs:
                    print(f"  partial outputs captured first: {result.partial_outputs}")
            case Failure():
                print("RESULT: FAILURE")
                print(f"  class:    {result.failure_class}")
                print(f"  step:     {result.step_id}")
                print(f"  phase:    {result.phase}")
                print(f"  expected: {result.expected}")
                print(f"  observed: {result.observed}")
        print("=" * 70)

        if args.headed and not args.no_pause:
            # Hold the browser at whatever screen the run ended on — that
            # final screen IS the evidence (the error banner, the
            # confirmation page), and it's gone the moment we close.
            input("\nBrowser is holding at the final screen. Press Enter to close... ")
    finally:
        driver.close()


if __name__ == "__main__":
    main()

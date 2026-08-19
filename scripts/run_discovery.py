"""Run a real LLM-driven discovery session against the local target app.

Usage:
    .venv/bin/python target_app/app.py &          # start the target app first
    .venv/bin/python scripts/run_discovery.py
    .venv/bin/python scripts/run_discovery.py --operator t.nguyen   # read-only
    .venv/bin/python scripts/run_discovery.py --operator a.novak    # no access

Requires ANTHROPIC_API_KEY in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()

import os

from computer_use.agent.discovery import DiscoveryAgent
from computer_use.artifact import build_capability, save_capability
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.evidence.recorder import EvidenceRecorder

# PARAMS are the concrete values used for THIS run. GOAL embeds them as plain
# text because the model has to act on real values — it has no notion of a
# "binding" at decision time. build_capability(), after success, substitutes
# any recorded literal that matches one of these values with ${input.<name>}
# — see artifact.py's module docstring.
PARAMS = {
    "member_id": "10234",
    "filter_account_type": "Savings",
    "account_type": "Money Market",
    "initial_deposit": "500",
}

# One flow that exercises every locator tier deliberately, rather than a
# happy path plus a separate side-test:
#   - search + "View"   -> ambiguous by name alone (global nav vs. page form,
#                          and 3 identical "View" links) -> needs near_text
#   - the filter icon   -> NO accessible name at all -> only the structural
#                          tier can reach it
#   - the balance cell  -> no name, no id -> needs row/column anchoring
# The resulting artifact therefore proves the ensemble on a real flow.
GOAL = (
    f"Look up member {PARAMS['member_id']} and open their detail page. The account table's "
    f"column headers have small icon-only controls (no text labels) for sorting and "
    f"filtering — work out which is which yourself. Use the filter control on the Account "
    f"Type column to show only {PARAMS['filter_account_type']} accounts, then read the "
    f"{PARAMS['filter_account_type']} balance. Finally, open a new {PARAMS['account_type']} "
    f"sub-account for this member with an initial deposit of ${PARAMS['initial_deposit']}, "
    f"and reach the confirmation screen."
)

BASE_URL = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5000")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--operator",
        default=os.environ.get("TARGET_APP_OPERATOR", "s.patel"),
        help="s.patel = Officer (read+write), t.nguyen = Teller (read only), "
        "a.novak = Unprovisioned (signs in, no access)",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--slow",
        type=int,
        default=300,
        metavar="MS",
        help="pause this many ms between actions so a headed run is watchable "
        "(default 300; use 0 for full speed)",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="close the browser immediately instead of holding it open at the "
        "final screen until you press Enter",
    )
    args = parser.parse_args()

    driver = PlaywrightDriver(BASE_URL)
    driver.start(headless=args.headless, slow_mo=args.slow)  # headed by default
    try:
        # Sign in BEFORE discovery starts, deliberately outside the recorded
        # flow. Authentication is an ambient property of the session, not a
        # step of the capability — baking credentials into a recorded artifact
        # would both leak them and make the capability unusable by any other
        # operator. Replay signs in the same way, before running the steps.
        operator = args.operator
        password = os.environ.get("TARGET_APP_PASSWORD", "demo")
        driver.goto("/login")
        driver._page.fill('input[name="username"]', operator)
        driver._page.fill('input[name="password"]', password)
        driver._page.click('input[type="submit"]')
        print(f"Signed in as: {operator}")

        # ~11 steps minimum for this flow; the extra headroom absorbs the
        # ambiguity retries the duplicate "Search"/"View" controls provoke.
        agent = DiscoveryAgent(driver, max_steps=22)
        print(f"Model: {agent.model}")
        print(f"Goal: {GOAL}\n")

        result = agent.run(GOAL, start_path="/")

        print("=" * 70)
        print("RESULT:", "SUCCESS" if result.ok else "STUCK")
        print("=" * 70)
        for step in result.steps:
            marker = "OK  " if step.ok else "FAIL"
            action_desc = ""
            if step.action:
                action_desc = f"{step.action.verb.value} -> {step.action.target}"
            print(f"[{marker}] step {step.seq:2d} | {step.intent}")
            if action_desc:
                print(f"          {action_desc}")
            if step.note:
                print(f"          note: {step.note}")

        print()
        print("Outputs:", result.outputs)
        if result.ok:
            print("Checkpoint (verified):", result.checkpoint_target)
            print("Summary:", result.summary)

            capability = build_capability(
                result,
                params=PARAMS,
                capability_id="read_balance_and_open_subaccount",
                description=(
                    "Look up a member, read their Savings balance, and open a new "
                    "sub-account for them, reaching the confirmation screen."
                ),
                target_surface=BASE_URL,
                entry_path="/",
                input_types={"initial_deposit": "number"},
            )
            path = save_capability(capability)
            print(f"\nCapability saved: {path}")
        else:
            print("Stuck reason:", result.stuck_reason)
            print("Explanation:", result.stuck_explanation)

        # Same recorder, same shapes, both modes — so a discovery trace and
        # a replay trace of the same flow can be read side by side.
        recorder = EvidenceRecorder(bindings=PARAMS)
        recorder.write_steps(result.steps)
        recorder.write_run(
            mode="discovery",
            capability_id="read_balance_and_open_subaccount",
            capability_version="1.0.0",
            operator=operator,
            inputs=PARAMS,
            extra={
                "model": agent.model,
                "goal": GOAL,
                "ok": result.ok,
                "llm_turns": len(result.steps),
                "stuck_reason": result.stuck_reason.value if result.stuck_reason else None,
                "outputs": {k: "recorded" for k in result.outputs},
            },
        )
        if not result.ok:
            recorder.write_failure_detail(driver.snapshot(), surface_id=driver._page.url)
        print(f"Evidence: {recorder.dir}/")

        if not args.headless and not args.no_pause:
            # Hold the browser at whatever screen the run ended on — that
            # final screen IS the evidence (the error banner, the
            # confirmation page), and it's gone the moment we close.
            input("\nBrowser is holding at the final screen. Press Enter to close... ")
    finally:
        driver.close()


if __name__ == "__main__":
    main()

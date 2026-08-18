"""Focused discovery test: can the model use the icon-only filter control?

This exercises the newest, hardest part of the target app: an icon button
with no meaningful accessible name, which only the structural locator tier
(anchor + "following:button") can resolve — see playwright_driver.py's
_resolve_structural.

Usage:
    .venv/bin/python target_app/app.py &
    .venv/bin/python scripts/run_discovery_filter_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()

import os

from computer_use.agent.discovery import DiscoveryAgent
from computer_use.drivers.playwright_driver import PlaywrightDriver

GOAL = (
    "Go to member 10234's detail page. The account table has small icon-only controls next "
    "to each column header for sorting and filtering — there is no visible text labeling "
    "them, so identify the right one yourself. Filter the account list to show only Savings "
    "accounts, then read the Savings balance to confirm the filter worked, then finish."
)

BASE_URL = os.environ.get("TARGET_APP_BASE_URL", "http://127.0.0.1:5000")


def main() -> None:
    driver = PlaywrightDriver(BASE_URL)
    driver.start(headless=False)
    try:
        agent = DiscoveryAgent(driver)
        print(f"Model: {agent.model}")
        print(f"Goal: {GOAL}\n")

        result = agent.run(GOAL, start_path="/member/10234")

        print("=" * 70)
        print("RESULT:", "SUCCESS" if result.ok else "STUCK")
        print("=" * 70)
        for step in result.steps:
            marker = "OK  " if step.ok else "FAIL"
            action_desc = f"{step.action.verb.value} -> {step.action.target}" if step.action else ""
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
        else:
            print("Stuck reason:", result.stuck_reason)
            print("Explanation:", result.stuck_explanation)
    finally:
        driver.close()


if __name__ == "__main__":
    main()

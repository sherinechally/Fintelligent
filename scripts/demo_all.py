"""Run the whole demo, either watchable or fast.

    .venv/bin/python scripts/demo_all.py            # watch it in Chromium
    .venv/bin/python scripts/demo_all.py --fast     # headless, ~20s, for CI

Boots both tenant installs, walks every outcome the system can produce, and
stops whatever it started — so reviewing this needs one terminal, not three.

Watched mode drives ONE Chromium window and captions each scenario on the
page itself: what is being demonstrated and what should happen. The caption
lives in a shadow root, so it cannot leak into the accessibility tree the
automation perceives — a demo that changed what the agent sees would be
demonstrating the demo.

No API key needed: this is all the deterministic path. A discovery run costs
money and is deliberately left out — see README.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from computer_use.artifact import load_capability
from computer_use.contracts import BusinessOutcome, Failure, Success
from computer_use.drivers.playwright_driver import PlaywrightDriver
from computer_use.replay.engine import ReplayEngine
from computer_use.tenants import TenantProfile, specialize

PYTHON = str(ROOT / ".venv" / "bin" / "python")
TENANTS = [("northbay", 5000), ("harborlight", 5001)]
NORTHBAY = "http://127.0.0.1:5000"
HARBORLIGHT = "http://127.0.0.1:5001"

SUBACCOUNT = ROOT / "capabilities" / "read_balance_and_open_subaccount__v1.0.0.json"
TRANSFER = ROOT / "capabilities" / "transfer_between_accounts__v1.0.0.json"

SUB_INPUTS = {
    "member_id": "10234",
    "filter_account_type": "Savings",
    "account_type": "Money Market",
    "initial_deposit": "500",
}
TRANSFER_INPUTS = {
    "member_id": "10234",
    "from_account": "...4471",
    "to_account": "...1029",
    "amount": "500",
}


# ---------------------------------------------------------------- servers


def port_is_serving(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=0.5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def start_tenants() -> list[subprocess.Popen]:
    """Start whichever tenants aren't already up.

    Reusing a server that is already serving matters: someone part-way
    through the README will have one running, and killing it out from under
    them is worse than joining it. Only what we started gets stopped.
    """
    started: list[subprocess.Popen] = []
    for tenant, port in TENANTS:
        if port_is_serving(port):
            print(f"  · {tenant} already serving on {port} — reusing it")
            continue
        started.append(
            subprocess.Popen(
                [PYTHON, str(ROOT / "target_app" / "app.py")],
                env={**os.environ, "TENANT": tenant, "PORT": str(port)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(ROOT / "target_app"),
            )
        )
        print(f"  · started {tenant} on {port}")

    # Wait for readiness rather than sleeping a guessed interval — a fixed
    # sleep is either slower than necessary or flaky on a loaded machine,
    # and usually both.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all(port_is_serving(p) for _, p in TENANTS):
            return started
        time.sleep(0.2)
    raise RuntimeError("tenant servers did not come up within 15s")


def stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


# -------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    title: str
    detail: str
    expect: str
    """The code or failure_class this scenario should produce."""
    artifact: Path = SUBACCOUNT
    inputs: dict = field(default_factory=lambda: dict(SUB_INPUTS))
    operator: str = "s.patel"
    surface: str = NORTHBAY
    expire_after: int = 0


SCENARIOS = [
    Scenario(
        "1/10  Happy path",
        "the recorded flow, replayed with no model in the loop",
        "success",
    ),
    Scenario(
        "2/10  Member not found",
        "a BUSINESS OUTCOME — a real answer, not a crash",
        "MEMBER_NOT_FOUND",
        inputs={**SUB_INPUTS, "member_id": "99999"},
    ),
    Scenario(
        "3/10  Closed account",
        "denial driven by the DATA — true for every operator",
        "ACCOUNT_CLOSED",
        inputs={**SUB_INPUTS, "member_id": "11002"},
    ),
    Scenario(
        "4/10  Deposit below the minimum",
        "the application's own validation, surfaced as an outcome",
        "DEPOSIT_BELOW_MINIMUM",
        inputs={**SUB_INPUTS, "initial_deposit": "5"},
    ),
    Scenario(
        "5/10  Ambiguous target",
        "3 members match — replay REFUSES to guess which account to open",
        "ambiguous_target",
        inputs={**SUB_INPUTS, "member_id": "1"},
    ),
    Scenario(
        "6/10  Operator not entitled",
        "denial driven by the IDENTITY — a provisioning defect, so a FAILURE",
        "not_entitled",
        operator="t.nguyen",
    ),
    Scenario(
        "7/10  Operator not provisioned",
        "signs in fine, has no entitlements at all — stops on screen one",
        "not_entitled",
        operator="a.novak",
    ),
    Scenario(
        "8/10  Session expires mid-flow",
        "re-authenticates, then refuses to resume a possibly-committed write",
        "recovery_exhausted",
        expire_after=3,
    ),
    Scenario(
        "9/10  High-value action, unattended",
        "our policy holds it; the app itself would have allowed it",
        "APPROVAL_REQUIRED",
        artifact=TRANSFER,
        inputs={**TRANSFER_INPUTS, "amount": "11000"},
    ),
    Scenario(
        "10/10  Same artifact, a different institution",
        "Harborlight relabelled the product; 4 lines of config absorb it",
        "success",
        surface=HARBORLIGHT,
    ),
]

HARBORLIGHT_PROFILE = TenantProfile(
    tenant_id="harborlight",
    display_name="Harborlight Federal",
    target_surface=HARBORLIGHT,
    label_map={
        "Search": "Find",
        "View": "Open",
        "Account Type": "Product",
        "Open New Sub-Account": "Add Sub-Account",
    },
)


def label_of(result) -> str:
    match result:
        case Success():
            return "success"
        case BusinessOutcome():
            return result.code
        case Failure():
            return result.failure_class.value
    return "?"


def run_scenario(sc: Scenario, headless: bool, slow: int) -> tuple[bool, str]:
    capability = load_capability(sc.artifact)
    if sc.surface == HARBORLIGHT:
        capability = specialize(capability, HARBORLIGHT_PROFILE)
    else:
        capability = capability.model_copy(update={"target_surface": sc.surface})

    driver = PlaywrightDriver(sc.surface)
    driver.start(headless=headless, slow_mo=slow)
    try:
        if not headless:
            driver.narrate(sc.title, sc.detail)
        driver.goto("/login")
        driver._page.fill('input[name="username"]', sc.operator)
        driver._page.fill('input[name="password"]', "demo")
        driver._page.click('input[type="submit"]')

        hook = None
        if sc.expire_after:
            fired: list[bool] = []

            def hook(seq: int, _n=sc.expire_after) -> None:
                if seq == _n and not fired:
                    fired.append(True)
                    driver._page.goto(f"{sc.surface}/expire-session")

        engine = ReplayEngine(driver, credentials=(sc.operator, "demo"), after_step=hook)
        result = engine.run(capability, sc.inputs)
        got = label_of(result)
        ok = got == sc.expect

        if not headless:
            driver.narrate(
                sc.title,
                f"{sc.detail}  →  {got}",
                tone="good" if isinstance(result, Success) else "stop",
            )
            time.sleep(2.2)  # let a watcher actually read the outcome
        return ok, got
    finally:
        driver.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fast",
        action="store_true",
        help="headless, no pauses — for CI or when you just want the result",
    )
    parser.add_argument("--slow", type=int, default=350, metavar="MS")
    parser.add_argument("--only", type=int, metavar="N", help="run scenario N only")
    args = parser.parse_args()

    print("Starting tenant installs...")
    procs = start_tenants()
    if not args.fast:
        print("\nA Chromium window will open and caption each scenario as it runs.")
    print()

    scenarios = [SCENARIOS[args.only - 1]] if args.only else SCENARIOS
    passed = failed = 0
    try:
        for sc in scenarios:
            ok, got = run_scenario(sc, headless=args.fast, slow=0 if args.fast else args.slow)
            print(f"  {'PASS' if ok else 'FAIL'}  {sc.title:<44} -> {got}")
            if ok:
                passed += 1
            else:
                failed += 1
                print(f"        expected {sc.expect}")
    finally:
        stop(procs)
        if procs:
            print(f"\nStopped {len(procs)} server(s) this script started.")

    print()
    print("=" * 72)
    print(f"  {passed} passed, {failed} failed")
    print("=" * 72)
    if not args.only:
        print("\nNot included (needs an API key, and costs money — run it deliberately):")
        print("    .venv/bin/python scripts/run_discovery.py")
        print("\nInteractive (you take control of the browser mid-run):")
        print("    .venv/bin/python scripts/run_handoff_demo.py")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

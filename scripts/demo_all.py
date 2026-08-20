"""Run the whole demo with one command.

Boots both tenant installs, walks every outcome the system can produce,
and tears the servers down again — so reviewing this doesn't require three
terminals and doesn't leave stray processes on ports 5000/5001.

No API key needed: everything here is the deterministic path. The one part
that DOES need a model — a discovery run — is deliberately left out, because
it costs money and should be a decision, not a side effect of running a
demo script. See README for that command.

    .venv/bin/python scripts/demo_all.py            # all of it
    .venv/bin/python scripts/demo_all.py --only policy
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PYTHON = str(ROOT / ".venv" / "bin" / "python")
TENANTS = [("northbay", 5000), ("harborlight", 5001)]


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
    them to start our own is worse than joining it. Only what we started
    gets stopped.
    """
    started: list[subprocess.Popen] = []
    for tenant, port in TENANTS:
        if port_is_serving(port):
            print(f"  · {tenant} already serving on {port} — reusing it")
            continue
        proc = subprocess.Popen(
            [PYTHON, str(ROOT / "target_app" / "app.py")],
            env={**os.environ, "TENANT": tenant, "PORT": str(port)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(ROOT / "target_app"),
        )
        started.append(proc)
        print(f"  · started {tenant} on {port}")

    # Wait for readiness rather than sleeping a guessed interval — a fixed
    # sleep is either slower than it needs to be or flaky on a loaded
    # machine, and usually both.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all(port_is_serving(port) for _, port in TENANTS):
            return started
        time.sleep(0.2)
    raise RuntimeError("tenant servers did not come up within 15s")


def stop(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        proc.send_signal(signal.SIGTERM)
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ------------------------------------------------------------------ demo


def run(label: str, expect: str, *args: str) -> bool:
    """Run one demo command and check it produced what the README claims."""
    result = subprocess.run(
        [PYTHON, *args], capture_output=True, text=True, cwd=str(ROOT)
    )
    output = result.stdout + result.stderr
    ok = expect in output
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} -> {expect}")
    if not ok:
        for line in output.strip().splitlines()[-8:]:
            print(f"          {line}")
    return ok


REPLAY = str(ROOT / "scripts" / "run_replay.py")
CROSS = str(ROOT / "scripts" / "run_cross_tenant_demo.py")

SECTIONS = {
    "replay": (
        "Deterministic replay — the production path, no LLM",
        [
            ("happy path", "RESULT: SUCCESS", REPLAY),
            ("member not found", "MEMBER_NOT_FOUND", REPLAY, "--member-id", "99999"),
            ("closed account", "ACCOUNT_CLOSED", REPLAY, "--member-id", "11002"),
            ("deposit below minimum", "DEPOSIT_BELOW_MINIMUM", REPLAY, "--deposit", "5"),
        ],
    ),
    "errors": (
        "Failures — the recording no longer matches reality",
        [
            ("ambiguous target (3 members match)", "ambiguous_target", REPLAY, "--member-id", "1"),
            ("operator not entitled", "not_entitled", REPLAY, "--operator", "t.nguyen"),
            ("operator not provisioned", "not_entitled", REPLAY, "--operator", "a.novak"),
            ("session expires mid-flow", "recovery_exhausted", REPLAY, "--expire-session-after", "3"),
        ],
    ),
    "policy": (
        "Policy — limits enforced in our layer, not the app's",
        [
            ("high-value, unattended", "APPROVAL_REQUIRED", REPLAY, "--deposit", "40000"),
        ],
    ),
    "tenants": (
        "Cross-tenant — one recording, two institutions",
        [
            ("same artifact, both installs", "One recording. Two institutions.", CROSS),
        ],
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=sorted(SECTIONS), help="run one section")
    args = parser.parse_args()

    print("Starting tenant installs...")
    procs = start_tenants()
    print()

    sections = {args.only: SECTIONS[args.only]} if args.only else SECTIONS
    passed = failed = 0
    try:
        for title, cases in sections.values():
            print("=" * 72)
            print(f"  {title}")
            print("=" * 72)
            for label, expect, *cmd in cases:
                if run(label, expect, *cmd):
                    passed += 1
                else:
                    failed += 1
            print()
    finally:
        stop(procs)
        if procs:
            print(f"Stopped {len(procs)} server(s) this script started.")

    print("=" * 72)
    print(f"  {passed} passed, {failed} failed")
    print("=" * 72)
    if not args.only:
        print("\nNot included (needs an API key, and costs money — run it deliberately):")
        print("    .venv/bin/python scripts/run_discovery.py")
        print("\nInteractive (you drive the browser):")
        print("    .venv/bin/python scripts/run_handoff_demo.py")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

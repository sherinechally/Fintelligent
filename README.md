# Computer-Use Automation System

An LLM discovers how to operate a legacy back-office UI **once**. That run becomes a typed,
versioned **capability artifact**. From then on the flow is replayed **deterministically, with no
model in the loop** — with an error taxonomy that separates real answers from real breakages, a
policy layer that holds risky actions, and a human handoff on the *same live session* when a
person genuinely needs to decide.

```
goal ──► LLM discovery ──► capability artifact ──► deterministic replay ──► result
         (~30s, cents)      (typed, reviewable)     (~0.5s, free)          success |
              │                                            │               business outcome |
              └──────────── evidence/ ─────────────────────┘               failure
                                                           │
                                                   policy holds a risky step
                                                           │
                                                   human decides, on the
                                                   same live browser session
```

---

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/playwright install chromium
```

Create `.env` from the template and add your Anthropic key:

```bash
cp .env.example .env
```

| Variable | Purpose | Needed for |
|---|---|---|
| `ANTHROPIC_API_KEY` | Discovery only | `run_discovery.py` |
| `ANTHROPIC_MODEL` | Defaults to `claude-sonnet-5` | discovery |
| `TARGET_APP_BASE_URL` | Defaults to `http://127.0.0.1:5000` | all |
| `TARGET_APP_OPERATOR` / `TARGET_APP_PASSWORD` | Mock app sign-in (`s.patel` / `demo`) | all |

**Everything except discovery runs with no API key.** Replay, policy, escalation, and the
evidence recorder never call a model — that is the point of the design, not a limitation of the
demo.

---

## Quickest look

One command. Boots both mock installs, walks every outcome the system can produce, tears the
servers down again. No API key needed — this is all the deterministic path.

```bash
.venv/bin/python scripts/demo_all.py
```

A Chromium window opens and **captions each scenario on the page** as it runs — what is being
demonstrated and what should happen. The caption is `aria-hidden`, so it is invisible to the
automation's own perception; a demo that changed what the agent sees would be demonstrating the
demo. (`tests/test_narration.py` pins that.)

Or skip the watching:

```bash
.venv/bin/python scripts/demo_all.py --fast     # headless, ~20s
.venv/bin/python scripts/demo_all.py --only 5   # just the ambiguous-target one
```

```
  PASS  happy path                                   -> RESULT: SUCCESS
  PASS  member not found                             -> MEMBER_NOT_FOUND
  PASS  closed account                               -> ACCOUNT_CLOSED
  PASS  deposit below minimum                        -> DEPOSIT_BELOW_MINIMUM
  PASS  ambiguous target (3 members match)           -> ambiguous_target
  PASS  operator not entitled                        -> not_entitled
  PASS  operator not provisioned                     -> not_entitled
  PASS  session expires mid-flow                     -> recovery_exhausted
  PASS  high-value, unattended                       -> APPROVAL_REQUIRED
  PASS  same artifact, both installs                 -> One recording. Two institutions.
```

The rest of this README is the same material one piece at a time, if you want to watch any of it
happen in a browser.

---

## Running it

(`demo_all.py` starts and stops these for you; do it by hand only if you want to click around
the app yourself.)

**1. Start the target app** (leave it running):

```bash
.venv/bin/python target_app/app.py
```

A deliberately legacy mock credit-union back office at `http://127.0.0.1:5000` — table layouts,
no test IDs, duplicate control names, icon-only buttons, three operator roles, session expiry.
All data is fictional.

**2. Discovery — the LLM drives the browser** (costs a few cents):

```bash
.venv/bin/python scripts/run_discovery.py
```

Watch it search a member, work out which unlabelled icon is the filter, read the balance, open a
sub-account, and reach confirmation. Saves
`capabilities/read_balance_and_open_subaccount__v1.0.0.json`.

**3. Replay the same flow — no LLM:**

```bash
.venv/bin/python scripts/run_replay.py --headed --slow 700
```

Drop `--headed --slow 700` to see production timing (~0.5s).

---

## The demo path

Run these in order; each shows a different part of the result contract.

```bash
.venv/bin/python scripts/run_replay.py
```
Success. Returns the extracted balance.

```bash
.venv/bin/python scripts/run_replay.py --member-id 99999
```
`MEMBER_NOT_FOUND` — a **business outcome**, not a crash.

```bash
.venv/bin/python scripts/run_replay.py --member-id 11002
```
`ACCOUNT_CLOSED` — data-driven denial. True for every operator.

```bash
.venv/bin/python scripts/run_replay.py --operator t.nguyen
```
`not_entitled` **failure** — identity-driven. A provisioning defect, not an answer about the
member. Same-looking screen, different category.

```bash
.venv/bin/python scripts/run_replay.py --member-id 1
```
`ambiguous_target` — three members match, three identical "View" links. Replay **refuses to
guess** rather than open the wrong member's account.

```bash
.venv/bin/python scripts/run_replay.py --expire-session-after 3
```
Session expires mid-flow. Replay re-authenticates, then declines to resume because this
capability isn't marked restartable — re-running it could repeat a committed write.

### Human-in-the-loop

```bash
.venv/bin/python scripts/run_handoff_demo.py
```

Moves $11,000 between a member's accounts. The **target app imposes no limit** — type any amount
and it posts. Our policy layer holds it at the committing step and hands you the browser, showing
the amount, the limit, the risk class, and the balance it just read, with the comparison computed
for you. You decide, holding the same live session; the automation cannot act until you answer.

```bash
.venv/bin/python scripts/run_handoff_demo.py --amount 500
```
Under the limit — never stops.

```bash
.venv/bin/python scripts/run_handoff_demo.py --amount 250000
```
Above the absolute ceiling — refused outright, and **no operator can approve past it**.

### Cross-tenant reuse

One recording, two institutions running the same product. Start the second tenant alongside the
first:

```bash
TENANT=harborlight PORT=5001 .venv/bin/python target_app/app.py
```

Harborlight has relabelled the product — "Search" is "Find", "View" is "Open", the Account Type
column is "Product". Same flow, different words. Then:

```bash
.venv/bin/python scripts/run_cross_tenant_demo.py
```

Runs the **same artifact** three ways: against Northbay (works), against Harborlight with no
config (fails, naming the exact control it couldn't find — while the steps anchored to unchanged
labels resolve anyway), and against Harborlight with its label map (works). Onboarding that
institution costs four lines of config, not a re-recording.

### Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

---

## Layout

```
src/computer_use/
  contracts.py          runtime types. ReplayResult = Success | BusinessOutcome | Failure
  artifact.py           the Capability: schema, parameterization, storage
  drivers/              perception (CDP accessibility tree) + acting + the session lease
  agent/discovery.py    the only place a model makes decisions
  replay/               deterministic executor + the known-states guard
  policy/               institutional limits, enforced here rather than by the app
  playbook.py           renders a capability as prose a non-engineer can check
  tenants.py            one capability, many institutions: label maps + specialization
  escalation/           intervention requests and live-session control transfer
  evidence/             structured run logs + redaction
target_app/             the mock back office being automated
capabilities/           saved artifacts, each with a generated .md playbook
tenants/                per-institution label maps
evidence/               run logs (redacted; committed deliberately)
```

`REPORT.md` covers the design decisions and their trade-offs.

---

## Notes

- **macOS users: AirPlay Receiver squats on port 5000.** If the target app isn't running, macOS
  answers `403` on that port instead of refusing the connection, so a stopped app looks like a
  protocol error. The driver detects this and tells you what to do. To avoid it entirely, turn off
  AirPlay Receiver (System Settings → General → AirDrop & Handoff), or run on another port:
  `PORT=5050 .venv/bin/python target_app/app.py` with
  `export TARGET_APP_BASE_URL=http://127.0.0.1:5050`.
- **The mock app's data resets when it restarts.** Transfers and new accounts are held in memory,
  so balances drift as you run demos. Restart the app for a clean slate.
- **Evidence is committed and redacted.** Values keep their shape and lose their content
  (`$##,###.##`); a typed value is recorded as the binding that drove it (`${input.member_id}`).
  Screenshots are gitignored — pixels of an account page cannot be redacted.
- **The artifacts in `capabilities/` were produced differently on purpose.**
  `read_balance_and_open_subaccount` came from a real LLM discovery run.
  `transfer_between_accounts` was hand-written (`scripts/make_transfer_capability.py`) — a
  capability is meant to be a reviewable document a person can read *and write*, not an opaque
  model output.

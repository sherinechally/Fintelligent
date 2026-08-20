# transfer_between_accounts

*Version 1.0.0 · recorded against http://127.0.0.1:5000*

> Generated from the capability artifact. Do not edit — regenerate it. A
> hand-maintained description drifts from what actually runs, and a stale
> playbook is worse than none, because it is still believed.

Move funds between two of a member's own accounts and reach the posted-transfer confirmation.

## What it needs

| Input | Type | Required |
|---|---|---|
| `member_id` | string | yes |
| `from_account` | string | yes |
| `to_account` | string | yes |
| `amount` | number | yes |

## What it gives back

| Output | Type |
|---|---|
| `source_balance_before` | string |

## What it does, step by step

Starting at `/`, signed in as whoever the caller
authenticated. Signing in is not part of this capability — credentials are
never recorded in an artifact.

1. **Type** the supplied `member_id` into the textbox on the page; failing that, the first textbox after the text **Member ID:** (found by position, not by name).

2. **Click** the button named **Search**; failing that, the first button after the text **Member ID:** (found by position, not by name).

3. **Click** the link named **View**.

4. **Read** the **Balance** cell of the row whose **Number** is the supplied `from_account`. Keep it as `source_balance_before`.

5. **Click** the link named **Transfer Funds**.

6. **Choose** the supplied `from_account` in the combobox on the page; failing that, the first combobox after the text **From Account:** (found by position, not by name).

7. **Choose** the supplied `to_account` in the combobox on the page; failing that, the first combobox after the text **To Account:** (found by position, not by name).

8. **Type** the supplied `amount` into the textbox on the page; failing that, the first textbox after the text **Transfer Amount** (found by position, not by name).

9. **Click** the button named **Post Transfer**.

   > ⚠️ **This is the step that commits.** Everything above it can be abandoned with no effect; this one has consequences. Policy is checked immediately before it, and a high-value action is held here for a person to authorise.

## How it knows it worked

It checks the screen for the text containing **Transfer posted successfully**. If that
is not there, the run is reported as a failure rather than a success — a
click is not evidence that the click did anything.

## If something goes wrong

- A legitimate answer — no such member, a closed account, insufficient funds,
  an amount over the limit — comes back as a **business outcome** with a code,
  not an error. Nothing is broken; that is the answer.
- A control that cannot be found, or that matches more than one thing, is a
  **failure**. The run stops rather than guessing which one to click.
- If the session expires part-way, it re-authenticates. Whether it may then
  re-run from the start is declared by this capability: **no** — re-running could repeat an action that already went through.

# read_balance_and_open_subaccount

*Version 1.0.0 · recorded against http://127.0.0.1:5000*

> Generated from the capability artifact. Do not edit — regenerate it. A
> hand-maintained description drifts from what actually runs, and a stale
> playbook is worse than none, because it is still believed.

Look up a member, read their Savings balance, and open a new sub-account for them, reaching the confirmation screen.

## What it needs

| Input | Type | Required |
|---|---|---|
| `member_id` | string | yes |
| `filter_account_type` | string | yes |
| `account_type` | string | yes |
| `initial_deposit` | number | yes |

## What it gives back

| Output | Type |
|---|---|
| `savings_balance` | string |

## What it does, step by step

Starting at `/`, signed in as whoever the caller
authenticated. Signing in is not part of this capability — credentials are
never recorded in an artifact.

1. **Type** the supplied `member_id` into the textbox on the page; failing that, the first textbox after the text **Member ID:** (found by position, not by name).

2. **Click** the button named **Search**; failing that, the first button after the text **Member ID:** (found by position, not by name).

3. **Click** the link named **View**.

4. **Click** the button named **Filter**; failing that, the first button after the text **Account Type** (found by position, not by name).

5. **Choose** the supplied `filter_account_type` in the combobox on the page.

6. **Read** the **Balance** cell of the row whose **Account Type** is the supplied `filter_account_type`. Keep it as `savings_balance`.

7. **Click** the link named **Open New Sub-Account**.

8. **Choose** the supplied `account_type` in the combobox on the page.

9. **Type** the supplied `initial_deposit` into the textbox on the page; failing that, the first textbox after the text **Initial Deposit ($):** (found by position, not by name).

10. **Click** the button named **Submit**.

   > ⚠️ **This is the step that commits.** Everything above it can be abandoned with no effect; this one has consequences. Policy is checked immediately before it, and a high-value action is held here for a person to authorise.

## How it knows it worked

It checks the screen for the text containing **Sub-account opened successfully**. If that
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

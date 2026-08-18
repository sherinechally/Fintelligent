"""A small, deliberately legacy-styled mock credit-union back office.

This is the ONE concrete surface the discovery agent and replay engine will
be built against. It's intentionally hostile in the ways real legacy
back-office apps are: table-based layout (not CSS), no `id`/`data-testid`
attributes anywhere, no semantic HTML5 elements. All data is fake.

Authentication is deliberately simplified but structurally faithful to what
these systems actually do: staff sign in, their ROLE decides which functions
they may invoke, and their session EXPIRES. In a real institution the
identity would come from Kerberos/AD SSO rather than this login form, and
entitlements from a directory group rather than a dict — but the three
runtime conditions that matter to automation are the same either way:

  - a denial driven by the DATA      (this member's account is closed)
  - a denial driven by the IDENTITY  (this operator lacks the entitlement)
  - the session/ticket EXPIRING mid-flow

Those are three different things and the automation must not conflate them —
see src/computer_use/replay/known_states.py.
"""

from __future__ import annotations

import time

from flask import Flask, abort, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = "mock-app-not-a-real-secret"  # local demo only; nothing sensitive

# Three operators, differing ONLY in entitlements — authentication succeeds
# for all three, and authorization is what separates them. Passwords are
# fake and compared in the clear because this is a mock; never a pattern to
# copy.
#
# The third one matters: a real directory is full of accounts that
# authenticate fine but carry no entitlements (newly created, or revoked
# while the account lives on). Automation pointed at such an account gets a
# clean login and then nothing — a failure mode worth handling explicitly,
# because "the credentials worked" makes it look like everything is fine.
OPERATORS = {
    "s.patel": {
        "password": "demo",
        "display": "S. Patel",
        "role": "Officer",
        "entitlements": {"MEMBER_VIEW", "SUBACCOUNT_CREATE"},
    },
    "t.nguyen": {
        "password": "demo",
        "display": "T. Nguyen",
        "role": "Teller",
        "entitlements": {"MEMBER_VIEW"},
    },
    "a.novak": {
        "password": "demo",
        "display": "A. Novak",
        "role": "Unprovisioned",
        "entitlements": set(),
    },
}

# Short on purpose so a session can plausibly expire during a demo run.
SESSION_TTL_SECONDS = 900


def current_operator() -> dict | None:
    """The signed-in operator, or None if signed out or expired."""
    username = session.get("username")
    if not username:
        return None
    if time.time() > session.get("expires_at", 0):
        return None
    return OPERATORS.get(username)


@app.before_request
def require_login():
    if request.endpoint in {"login", "static", "expire_session"}:
        return None
    if current_operator() is None:
        # Distinguish "never signed in" from "your session ran out" — the
        # second is a RECOVERABLE condition for automation (re-auth and
        # resume), the first is not.
        expired = bool(session.get("username"))
        session.clear()
        return redirect(url_for("login", expired="1" if expired else None))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        operator = OPERATORS.get(username)
        if operator and operator["password"] == password:
            session["username"] = username
            session["expires_at"] = time.time() + SESSION_TTL_SECONDS
            return redirect(url_for("search"))
        error = "Sign-in failed. Check your credentials and try again."
    return render_template(
        "login.html", error=error, expired=request.args.get("expired") == "1"
    )


@app.context_processor
def inject_operator():
    """Every page's header shows who is signed in — which is also what
    makes UiSnapshot.principal observable to the automation."""
    return {"operator": current_operator()}


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/expire-session")
def expire_session():
    """Test affordance: force the current session to look expired.

    Exists so a replay can be made to hit a mid-flow session expiry
    deterministically, instead of waiting out SESSION_TTL_SECONDS. Clearly
    named, and it only ever destroys the caller's own session.
    """
    session["expires_at"] = 0
    return redirect(url_for("login", expired="1"))

# Fake, fictional member records — PII-shaped on purpose (see the
# conversation in REPORT.md's Safety section): the live UI shows real-looking
# name/balance data, same as a real back-office tool would. The automation
# system's evidence/artifact layer is what's responsible for redacting this
# before it hits disk, not the app withholding it up front.
MEMBERS = {
    "10234": {
        "id": "10234",
        "name": "J. Alvarez",
        "status": "Active",
        "accounts": [
            {"type": "Savings", "number": "...4471", "balance": 12340.55},
            {"type": "Checking", "number": "...1029", "balance": 2108.40},
            {"type": "Certificate", "number": "...0093", "balance": 5000.00, "maturity": "2027-03-01"},
        ],
    },
    "10771": {
        "id": "10771",
        "name": "R. Chen",
        "status": "Active",
        "accounts": [
            {"type": "Savings", "number": "...7720", "balance": 875.10},
            {"type": "Checking", "number": "...3391", "balance": 412.02},
        ],
    },
    "11002": {
        "id": "11002",
        "name": "M. Okafor",
        "status": "Closed",
        "accounts": [
            {"type": "Savings", "number": "...5560", "balance": 0.00},
        ],
    },
}


def deny_if_missing(entitlement: str):
    """Return a 403 page if the signed-in operator lacks `entitlement`.

    Applied to READ routes as well as write ones, so an account with no
    entitlements at all is blocked at the very first screen rather than
    silently seeing an empty app.
    """
    operator = current_operator()
    if entitlement in operator["entitlements"]:
        return None
    return render_template("not_entitled.html", operator=operator, entitlement=entitlement), 403


@app.route("/")
def search():
    denied = deny_if_missing("MEMBER_VIEW")
    if denied:
        return denied

    query = request.args.get("member_id", "").strip()
    searched = query != ""
    results = []
    if searched:
        # Partial match on ID or name, not just an exact key lookup — a real
        # search box would do this, and it's also what gives the results
        # table a reason to ever show more than one row (see the "View"
        # link ambiguity this creates, and how structural locators resolve
        # it — REPORT.md's Determinism section).
        q = query.lower()
        results = [m for m in MEMBERS.values() if q in m["id"] or q in m["name"].lower()]
    return render_template("search.html", query=query, results=results, searched=searched)


ACCOUNT_COLUMNS = [("type", "Account Type"), ("number", "Number"), ("balance", "Balance"), ("maturity", "Maturity")]


@app.route("/member/<member_id>")
def detail(member_id: str):
    denied = deny_if_missing("MEMBER_VIEW")
    if denied:
        return denied

    member = MEMBERS.get(member_id)
    if member is None:
        abort(404)

    accounts = list(member["accounts"])

    filter_col = request.args.get("filter_col")
    filter_value = request.args.get("filter_value")
    if filter_col and filter_value and filter_value != "All":
        accounts = [a for a in accounts if str(a.get(filter_col, "")) == filter_value]

    sort_col = request.args.get("sort_col")
    sort_dir = request.args.get("sort_dir", "asc")
    if sort_col:
        accounts = sorted(
            accounts,
            key=lambda a: a.get(sort_col) if a.get(sort_col) is not None else "",
            reverse=(sort_dir == "desc"),
        )

    # Only one column's filter dropdown is ever open at a time — clicking a
    # different column's filter icon closes whichever was open before.
    show_filter_for = request.args.get("show_filter_for")
    filter_options: list[str] = []
    if show_filter_for:
        values = {
            str(a[show_filter_for]) for a in member["accounts"] if a.get(show_filter_for) is not None
        }
        filter_options = ["All"] + sorted(values)

    return render_template(
        "detail.html",
        member=member,
        accounts=accounts,
        may_create_subaccount="SUBACCOUNT_CREATE" in current_operator()["entitlements"],
        columns=ACCOUNT_COLUMNS,
        sort_col=sort_col,
        sort_dir=sort_dir,
        filter_col=filter_col,
        filter_value=filter_value,
        show_filter_for=show_filter_for,
        filter_options=filter_options,
    )


ACCOUNT_TYPES = ["Savings", "Money Market"]
MIN_INITIAL_DEPOSIT = 25.00


@app.route("/member/<member_id>/new-subaccount", methods=["GET", "POST"])
def new_subaccount(member_id: str):
    member = MEMBERS.get(member_id)
    if member is None:
        abort(404)

    # TWO different denials, deliberately distinguishable. Automation that
    # treats them the same is wrong in an important way: the first is the
    # true answer for everyone, the second means THIS operator can't do it
    # but another one could.
    denied = deny_if_missing("SUBACCOUNT_CREATE")
    if denied:
        return denied

    if member["status"] != "Active":
        # Enforced here too, not just hidden in the UI — an agent (or a
        # replay) that navigates straight to this URL must still be denied.
        return render_template("permission_denied.html", member=member), 403

    error = None
    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        raw_amount = request.form.get("initial_deposit", "").strip()

        try:
            amount = float(raw_amount)
        except ValueError:
            amount = None

        if account_type not in ACCOUNT_TYPES:
            error = f'Invalid account type "{account_type}".'
        elif amount is None:
            error = f'"{raw_amount}" is not a valid dollar amount.'
        elif amount < MIN_INITIAL_DEPOSIT:
            error = f"Initial deposit must be at least ${MIN_INITIAL_DEPOSIT:.2f}."
        else:
            new_account_number = f"...{1000 + len(member['accounts']):04d}"
            member["accounts"].append(
                {"type": account_type, "number": new_account_number, "balance": amount}
            )
            reference = f"SA-{member_id}-{len(member['accounts']):03d}"
            return render_template(
                "confirmation.html",
                member=member,
                account_type=account_type,
                amount=amount,
                account_number=new_account_number,
                reference=reference,
            )

    return render_template(
        "new_subaccount.html",
        member=member,
        account_types=ACCOUNT_TYPES,
        error=error,
    )


if __name__ == "__main__":
    # use_reloader=False: the reloader spawns a second child process, which
    # made it easy to lose track of which process actually owned port 5000
    # during manual testing. Single process only; restart manually on edits.
    app.run(port=5000, debug=True, use_reloader=False)

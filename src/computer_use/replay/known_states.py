"""What the target app looks like when something noteworthy has happened.

Checked after EVERY step, not per-step. Session timeouts, permission
denials, and validation errors can appear at any point in a flow — modelling
them as "the thing I expect after step 4" guarantees you miss the one that
shows up after step 7. A global guard costs one extra scan per step and
catches all of them.

Deliberately declarative and defined ONCE per target app, not per
capability: "here's what a permission denial looks like in this app" is a
property of the app, and every capability that runs against it should
inherit that knowledge rather than re-derive it. In a multi-tenant
deployment this table is what you'd specialize per vendor product — see
REPORT.md's Heterogeneity section.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class StateKind(StrEnum):
    """Three kinds, because "permission denied" is not one condition.

    A denial driven by the DATA and a denial driven by the CALLER'S IDENTITY
    look almost identical on screen and mean completely different things:

      - "this member's account is closed"     -> true for every operator.
        The answer is no. Nobody should retry. BUSINESS_OUTCOME.

      - "your role lacks SUBACCOUNT_CREATE"   -> true for THIS operator only.
        Another identity would succeed. The capability is provisioned
        against the wrong account — a configuration defect someone must fix.
        Returning it as a business outcome would let a mis-provisioned
        capability fail quietly forever, looking like a stream of legitimate
        "no"s. CONFIG_FAILURE.

      - "your session expired"                -> nothing is wrong with the
        request at all; re-authenticate and continue. RECOVERABLE.
    """

    BUSINESS_OUTCOME = "business_outcome"
    """A legitimate answer the caller needs. Stop, report it as a result."""

    CONFIG_FAILURE = "config_failure"
    """The request was sound but this identity may not perform it. A hard
    failure aimed at whoever provisions the capability, not at the caller."""

    RECOVERABLE = "recoverable"
    """Transient or dismissable. Try the documented recovery, then continue."""


class KnownState(BaseModel):
    """One recognizable page condition.

    `text_signature` is matched against the page's visible text. Crude on
    purpose: it's the one signal available across every surface this design
    targets (a desktop app or a terminal screen has no status code to read),
    so the mechanism stays the same when the driver changes.
    """

    model_config = ConfigDict(frozen=True)
    code: str
    kind: StateKind
    text_signature: str
    message: str = ""


# Order matters only for reporting — the first match wins, so put the more
# specific signatures first.
DEFAULT_KNOWN_STATES: tuple[KnownState, ...] = (
    KnownState(
        code="MEMBER_NOT_FOUND",
        kind=StateKind.BUSINESS_OUTCOME,
        text_signature="No member found matching",
        message="No member matches the supplied member_id.",
    ),
    # --- identity-driven: NOT a business outcome (see StateKind) ---
    KnownState(
        code="SESSION_EXPIRED",
        kind=StateKind.RECOVERABLE,
        text_signature="Your session has expired",
        message="The operator session expired mid-flow; re-authentication is required.",
    ),
    # Two DIFFERENT provisioning defects with different remediations, so
    # they get different codes even though both are CONFIG_FAILURE:
    # "grant this role one more entitlement" vs. "this account was never
    # set up for this application at all". More specific signature first.
    KnownState(
        code="OPERATOR_NOT_PROVISIONED",
        kind=StateKind.CONFIG_FAILURE,
        text_signature="has no entitlements for Member Services",
        message=(
            "The signed-in account authenticates but has no entitlements for this "
            "application. It was never provisioned for it (or was revoked). "
            "Automation should not be pointed at this account."
        ),
    ),
    KnownState(
        code="NOT_ENTITLED",
        kind=StateKind.CONFIG_FAILURE,
        text_signature="Not Entitled",
        message=(
            "The signed-in operator lacks the entitlement this capability requires. "
            "This is a provisioning defect, not an answer about the member."
        ),
    ),
    KnownState(
        code="NOT_ENTITLED",
        kind=StateKind.CONFIG_FAILURE,
        text_signature="Your role does not permit",
        message=(
            "The signed-in operator lacks the entitlement this capability requires. "
            "This is a provisioning defect, not an answer about the member."
        ),
    ),
    # --- data-driven: genuinely a business outcome ---
    #
    # ONE condition, TWO ways the app expresses it: a hard 403 page if you
    # navigate straight to the action URL, or — the path replay actually
    # takes — the detail page simply not rendering the link at all. Only the
    # first was in this table initially, so replay hit the second and
    # reported "target not found": a system failure, for what is actually a
    # legitimate answer. Exactly the conflation the brief calls the most
    # common mistake here, and it only surfaced by running the flow against
    # a closed member.
    KnownState(
        code="ACCOUNT_CLOSED",
        kind=StateKind.BUSINESS_OUTCOME,
        text_signature="Access Denied",
        message="This member's account is closed; sub-accounts cannot be opened.",
    ),
    KnownState(
        code="ACCOUNT_CLOSED",
        kind=StateKind.BUSINESS_OUTCOME,
        text_signature="Account is closed",
        message="This member's account is closed; sub-accounts cannot be opened.",
    ),
    KnownState(
        code="DEPOSIT_BELOW_MINIMUM",
        kind=StateKind.BUSINESS_OUTCOME,
        text_signature="Initial deposit must be at least",
        message="The supplied initial_deposit is below the institution's minimum.",
    ),
    KnownState(
        code="INVALID_AMOUNT",
        kind=StateKind.BUSINESS_OUTCOME,
        text_signature="is not a valid dollar amount",
        message="The supplied initial_deposit is not a valid amount.",
    ),
)


def match_known_state(
    page_text: str, states: tuple[KnownState, ...] = DEFAULT_KNOWN_STATES
) -> KnownState | None:
    for state in states:
        if state.text_signature in page_text:
            return state
    return None

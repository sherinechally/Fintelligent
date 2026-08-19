"""Institutional guardrails, enforced in OUR layer.

The target application has no upper limit on a transfer: type a number, it
moves the money. That is realistic — plenty of back-office screens will do
whatever the operator asks, with limits enforced by procedure around the
system rather than inside it. Which is exactly why the automation layer
cannot rely on the application to stop anything.

Two ideas do the work here:

  1. RISK IS VALUE-DEPENDENT. A $500 transfer and a $75,000 transfer are the
     same verb, the same screen, the same recorded step. Classifying by
     action TYPE alone cannot tell them apart, so the amount has to be part
     of the classification. Rules compare bound input values, not verbs.

  2. NOT EVERY LIMIT IS THE SAME KIND OF LIMIT. Some are absolute (nobody
     moves half a million through this capability, and no operator standing
     next to the terminal can wave it through). Others exist to force a
     second pair of eyes. Collapsing those into one "blocked" would either
     make hard limits soft or make routine approvals impossible, so the rule
     declares which it is.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RiskClass(StrEnum):
    """Assigned by this engine at authorization time — never declared by the
    model, and never carried in from the artifact author's intent. If a
    caller could label its own action safe, the classification would be
    worth nothing."""

    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    """Creates something that can be undone or ignored — opening an account
    that can be closed again."""

    WRITE_IRREVERSIBLE = "write_irreversible"
    """Moves money. There is no 'undo transfer' button; reversing it is a
    new transaction with its own approval, and the member has already seen
    the balance change."""


class Disposition(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    """Escalate to a human, who decides with the run's context in front of
    them. The action is permitted — it just isn't permitted UNATTENDED."""

    DENY = "deny"
    """Absolute. No in-band override: an operator at the console cannot
    approve past this, because the whole point of a hard limit is that it
    doesn't bend to whoever happens to be on shift. Raising it is a change
    to policy, reviewed and deployed, not a decision made under time
    pressure mid-run."""


class AmountRule(BaseModel):
    """"For capability X, if input Y exceeds Z, do W."

    Deliberately declarative and boring. A rule a compliance reviewer can
    read without reading Python is worth more than an expressive one they
    can't audit.
    """

    model_config = ConfigDict(frozen=True)
    capability_id: str = "*"
    input_name: str
    above: float
    disposition: Disposition
    risk_class: RiskClass = RiskClass.WRITE_IRREVERSIBLE
    reason: str = ""


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    disposition: Disposition
    risk_class: RiskClass
    reason: str = ""
    rule_input: str | None = None
    rule_threshold: float | None = None
    observed_value: float | None = None

    @property
    def needs_human(self) -> bool:
        return self.disposition is Disposition.REQUIRE_APPROVAL


# Thresholds chosen to be demonstrable rather than to model any real
# institution's limits. In a deployment this file is per-tenant
# configuration, not code — see REPORT.md (Heterogeneity).
DEFAULT_RULES: tuple[AmountRule, ...] = (
    # Absolute ceiling. Nobody waves this through in-band.
    AmountRule(
        capability_id="*",
        input_name="amount",
        above=100_000.0,
        disposition=Disposition.DENY,
        risk_class=RiskClass.WRITE_IRREVERSIBLE,
        reason="Transfer exceeds the maximum this system will perform under any authority.",
    ),
    # Second pair of eyes.
    AmountRule(
        capability_id="*",
        input_name="amount",
        above=10_000.0,
        disposition=Disposition.REQUIRE_APPROVAL,
        risk_class=RiskClass.WRITE_IRREVERSIBLE,
        reason="Transfer above the unattended limit; requires human authorisation.",
    ),
    # Opening an account is reversible, so its bar sits higher than a
    # transfer's — the risk is the money, not the account.
    AmountRule(
        capability_id="*",
        input_name="initial_deposit",
        above=25_000.0,
        disposition=Disposition.REQUIRE_APPROVAL,
        risk_class=RiskClass.WRITE_REVERSIBLE,
        reason="Opening deposit above the unattended limit; requires human authorisation.",
    ),
)


def _as_amount(raw: str | float | None) -> float | None:
    """Parse a money-ish input, or None if it isn't a number at all.

    An unparseable amount ("abc") deliberately does NOT escalate. It is a
    typo, and the application's own validation rejects it — that path is a
    business outcome the caller should see, not a page to a human. This
    engine exists to catch amounts that are perfectly VALID and too large;
    amounts that are invalid belong to the app's validation, and routing
    them here would train operators to click through approvals.
    """
    if raw is None:
        return None
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def evaluate(
    capability_id: str,
    inputs: dict[str, str],
    rules: tuple[AmountRule, ...] = DEFAULT_RULES,
) -> PolicyDecision:
    """Classify an invocation from its capability and its INPUT VALUES.

    Evaluated strictly: DENY beats REQUIRE_APPROVAL beats ALLOW, so adding a
    lower approval threshold can never accidentally soften a hard ceiling
    above it, whatever order the rules happen to be listed in.
    """
    matched: list[tuple[AmountRule, float]] = []
    for rule in rules:
        if rule.capability_id not in ("*", capability_id):
            continue
        value = _as_amount(inputs.get(rule.input_name))
        if value is not None and value > rule.above:
            matched.append((rule, value))

    for wanted in (Disposition.DENY, Disposition.REQUIRE_APPROVAL):
        for rule, value in matched:
            if rule.disposition is wanted:
                return PolicyDecision(
                    disposition=rule.disposition,
                    risk_class=rule.risk_class,
                    reason=rule.reason,
                    rule_input=rule.input_name,
                    rule_threshold=rule.above,
                    observed_value=value,
                )

    return PolicyDecision(disposition=Disposition.ALLOW, risk_class=RiskClass.READ)

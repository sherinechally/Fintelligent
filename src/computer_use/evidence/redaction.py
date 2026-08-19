"""Keep regulated data out of anything we persist.

The governing idea: evidence records what the AUTOMATION DID, not what the
screen said. "Step 6 resolved a Balance cell via a row anchor and read a
value shaped like money" is everything an engineer needs to debug a locator
or a flow. The member's actual balance adds nothing to that and is exactly
the thing that must not accumulate in a log directory.

That framing is what makes redaction tractable. Trying to spot a person's
name in arbitrary page text is a losing game — names look like other words.
Not logging page text wholesale means there is no name to spot.

Where a value must be represented at all, we keep its SHAPE and drop its
content: "$12,340.55" becomes "$##,###.##". Shape is what tells you the read
worked and returned money rather than an empty cell or an error string;
content is what tells you about a member.
"""

from __future__ import annotations

import re

_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
_MASKED_ACCOUNT = re.compile(r"\.{3}\d{3,4}")
_LONG_DIGITS = re.compile(r"\b\d{4,}\b")


def _shape_of_money(match: re.Match[str]) -> str:
    return "$" + re.sub(r"\d", "#", match.group(0).lstrip("$ ").strip())


def redact(text: str | None) -> str | None:
    """Mask money, account numbers, and long digit runs, preserving shape.

    Long digit runs cover member ids and anything id-like. Four digits is
    the threshold because shorter numbers are overwhelmingly counts, column
    positions, and step numbers — redacting those would blind the log
    without protecting anybody.
    """
    if text is None:
        return None
    out = _MONEY.sub(_shape_of_money, text)
    out = _MASKED_ACCOUNT.sub("...####", out)
    out = _LONG_DIGITS.sub(lambda m: "#" * len(m.group(0)), out)
    return out


def redact_value(value: str | None, bindings: dict[str, str] | None = None) -> str | None:
    """Represent a value that was typed into the application.

    Prefers naming the BINDING it came from over masking the literal: a log
    line reading `${input.member_id}` is both safer and more useful than
    `#####`, because it says which parameter drove the step. The artifact
    already stores steps in exactly this form; this restores that view for a
    run where the values have been bound to real data.
    """
    if value is None:
        return None
    for name, bound in (bindings or {}).items():
        if bound and str(bound) == value:
            return f"${{input.{name}}}"
    return redact(value)

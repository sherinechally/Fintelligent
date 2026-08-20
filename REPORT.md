# Design write-up

## 1. Architecture

The system has one load-bearing seam: **a capability describes intent against controls; a driver
knows how to perceive and act on a surface; neither knows about the other.** Everything else —
desktop support, multi-tenant reuse, drift detection — falls out of that.

```
DiscoveryAgent ─┐                                   ┌─ PolicyEngine (limits)
                ├─► Action (a proposal) ─► Driver ──┤
ReplayEngine  ──┘                          act()    └─ Lease (who may act)
```

Four decisions worth defending:

**Perception reads the browser's accessibility tree directly via CDP, not a library helper.** The
hard problem in this environment is surfaces with no clean DOM. Leaning on `get_by_role` would
mean trusting Playwright to have already solved "make sense of a messy screen" — the exact part
that has to generalise to a desktop app where that helper doesn't exist. So we consume the raw
tree and normalise it ourselves into `UiNode`. Acting is different: once we know *which* control
we want, Playwright's locator engine is the right tool and reinventing it buys nothing. Perception
and acting deliberately use different mechanisms because they solve different problems.

**`Action` is a proposal, not a command.** Both the LLM and the replay engine emit `Action`s;
neither can execute one. Only the driver can, and only after policy and the lease agree. The model
can ask for anything and reach nothing on its own authority — that asymmetry is the safety model,
and it exists because "decide" and "do" are separate objects.

**One driver owns the lease.** `act()` is the single place anything touches the page, so the
"who may act right now" check lives there. Scattering that check across the agent loop, the replay
engine, and the escalation handler would make it a convention every caller has to remember.

**Trade-off accepted:** a single process, synchronous, no queue. The brief explicitly does not
reward scaling infrastructure, and a queue would have obscured the seam that actually matters.

---

## 2. Artifact schema

A `Capability` (see `artifact.py`) carries: typed `inputs`, typed `outputs`, ordered `steps` each
with a `ControlRef`, a structured `checkpoint`, `commit_step_id`, and
`restartable_after_recovery`.

**Parameterization happens after the fact, not during discovery.** The model always acts on real
values — it has to, to operate the page. Once a run succeeds, `build_capability` replaces any
recorded literal matching an input value with `${input.<name>}`, including nested ones (a row
anchor's `equals` becomes `${input.filter_account_type}`). The artifact is therefore free of the
data that produced it by construction rather than by a scrubbing pass.

**Only steps that actually succeeded are persisted.** The brief asks for "the successful run …
decoupled from the raw model transcript", and this filter is what makes that true. Our own
transcript had the model guessing the search box was named `"Member ID:"` (it has no accessible
name; the label sits in a separate cell), failing, and correcting next turn. Both attempts were
initially written to the artifact, so replay reproduced the mistake with perfect fidelity and died
on step 1. Decoupling from the transcript is about *filtering*, not just format.

**The checkpoint is structured, and discovery verifies its own claim.** `finish_success` requires
a re-checkable reference, which is resolved against the live screen before success is accepted. A
model asserting "I reached the confirmation page" is not evidence; the same locator machinery
replay uses is.

**Reviewable means readable, so the artifact is also rendered as prose.**
`playbook.py` writes a `.md` beside each capability: what it needs, what it does step by step,
which control each step targets and *whether it is relying on position rather than a name*, which
single step commits, and what happens when things go wrong. §3.2 asks for an artifact reviewable
by "both a human reviewer and a calling agent" — JSON serves the agent well and the reviewer
badly, and approving banking automation should not require reading nested `ControlRef` objects.
It is generated, never edited: a hand-maintained description drifts from what runs, and a stale
one is worse than none because it is still believed.

**`recorded_tier` is the drift baseline.** Each step records which locator tier resolved it *at
record time*. Without it, every fallback looks like drift: our artifact legitimately records four
steps as `structural`, because those controls were ambiguous from the start. Drift is a *change*
from the baseline, which is a signal worth acting on.

---

## 3. Determinism & error handling

**Locators are an ensemble, tried in order, with the winner recorded.** Tier 1 is semantic
(role + accessible name). Tier 2 is structural — anchored, not absolute: *"the button following
the text 'Member ID:'"*. Anchoring is what makes it survive change, and it is the only tier that
can reach the app's icon-only filter buttons, which have no accessible name at all. Tier 3
(visual template matching) is in the schema and **not implemented**.

For table data, `RowAnchor` expresses *"the Balance cell in the row whose Account Type is
Savings"* — matched by header **text**, not column index, so it survives reordering.

**Ambiguity is a hard failure, never "take the first match."** Searching `1` matches three
members, so the recorded "View" link matches three times; replay stops. In a back office,
"two controls matched so I picked one" is how you post against the wrong account. This is also an
honest limitation of record-once/replay-many surfacing itself: that step has no structural
fallback *because the recorded search returned exactly one row*, and replay's job is to notice,
not improvise.

**Known states are checked globally, after every step** (`known_states.py`) — not as per-step
expectations. Session expiry and permission denials can appear anywhere; modelling them per step
guarantees missing the one you didn't predict. The table is a property of the *application*, so
discovery consumes it too: an unentitled discovery run halts in **one** API call instead of
burning twenty-two hunting for a control that will never render.

**Three categories, and the test for which is: does somebody have to fix something?**

| Condition | Result | Why |
|---|---|---|
| `MEMBER_NOT_FOUND`, `ACCOUNT_CLOSED`, `INSUFFICIENT_FUNDS` | BusinessOutcome | A real answer. Nothing to fix. |
| `APPROVAL_DECLINED`, `APPROVAL_REQUIRED`, `EXCEEDS_MAXIMUM` | BusinessOutcome | A limit applied or a reviewer judged. The control working. |
| `not_entitled` | Failure | Provisioning is wrong. Someone must fix it. |
| `ambiguous_target`, `checkpoint_failed` | Failure | The recording no longer matches reality. |

Two of these cost real bugs to get right. **`ACCOUNT_CLOSED` has two distinct UI manifestations** —
a 403 page if you navigate straight to the URL, or the link simply not rendering on the path
replay actually takes. Only the first was covered, so replay reported `target_not_found`: a system
failure, for a legitimate answer. Writing known states by reading templates systematically misses
the manifestations your capabilities actually walk into.

And **policy stops were initially `Failure`s** — so declining a transfer produced "RESULT:
FAILURE", which reads as though something broke when in fact the four-eyes control worked exactly
as designed. `APPROVAL_DECLINED` and `APPROVAL_REQUIRED` are also deliberately distinct: only one
of them is worth retrying.

**Waiting** is Playwright's own auto-waiting plus bounded retries. No `sleep`.

---

## 4. Heterogeneity & multi-tenant

**The seam is `UiNode` / `ControlRef`.** A capability never names a CSS selector or a DOM tag. It
says *"the button named Post Transfer"* or *"the Balance cell in the Savings row"*. Translating
that into this app's actual HTML happens in exactly one place — `_ROLE_TAG_XPATH` in the
Playwright driver. A different surface means a new driver implementing perceive/resolve/act; the
artifact, the replay engine, the policy layer, and the escalation model are untouched.

`StructuralRef.path` is a small abstract vocabulary (`"following:button"`) rather than raw XPath
precisely to keep that knowledge behind the seam. If the path held `input[type=submit]`, DOM
trivia would leak into the artifact layer and the abstraction would be decorative.

**Desktop and terminal surfaces.** A Windows UIA element already fits `UiNode` — role, name,
value, states is the greatest common factor by design. The interesting case is a **3270/5250
terminal**, and it inverts an assumption worth stating: on the web, coordinates are the least
durable locator, so the tier order is semantic → structural → visual. On a green screen the layout
is a fixed 24×80 grid defined by a map that often hasn't changed in decades, so `(row, col)` is the
*most* stable strategy available and the order flips. That is exactly why `ControlRef` stores
which tiers exist but **not their precedence** — precedence is a property the driver declares,
because it is a property of the surface. A capability recorded on the web must not carry the web's
assumptions.

**Multi-tenant reuse — built and demonstrated.** `scripts/run_cross_tenant_demo.py` runs one
recorded capability against two institutions running the same product, where the second has
relabelled it (Search→Find, View→Open, the Account Type column→Product, the sub-account
link→Add Sub-Account). The mock app is one codebase driven by a tenant config, because that is
how a vendor product is actually deployed — not a second app pretending to be one.

The overrides are a **label map**, not per-step patches:

```json
{"Search": "Find", "View": "Open", "Account Type": "Product"}
```

Per-step patches (`step_2: use this locator`) tie overrides to step *numbers*, so every tenant's
config silently rots the next time the base is re-recorded. A label map is stated in the base's
own vocabulary: it survives re-recording, applies wherever the label occurs, and is legible to
whoever administers the tenant rather than only to whoever wrote the automation. A renamed
**column header** is handled the same way, because a row anchor matches on header text — and the
row's *value* is deliberately not rewritten, since that is member data rather than a label.
`step_overrides` remains for a control that genuinely moved rather than merely got renamed.

The demo deliberately runs the un-overridden case too, and it is the most informative of the
three: against Harborlight with no config, `step_1` still resolves — it is anchored to
"Member ID:", which that tenant did not rename — and the run fails at `step_2` naming the exact
control it could not find. Some drift is absorbed by the structural tier for free; the rest costs
four lines. **Onboarding this institution: one recording, four lines of config.**

**Drift detection is already free.** `recorded_tier` versus what actually resolves tells you
tenant 042 has drifted *while the fallback is still holding* — before it breaks.

Still config-as-code rather than a service: `known_states.py` and `policy/rules.py` would become
per-tenant data alongside the label map. Both are already plain declarative tables for that
reason.

---

## 5. Escalation & handoff

**Detecting stuck** reuses machinery rather than adding a parallel path: unresolvable ambiguity,
recovery exhaustion, and — the case that actually matters — a policy hold on a risky action.

An earlier version escalated on locator ambiguity, and that was a **poor use of a person**: three
partial-ID matches tell a human nothing the machine didn't already know. Escalation should be for
*judgment*. The demo now holds a high-value transfer, which is a call a person can actually make.

**The human takes over the same live session.** The automation drives a headed browser; the
operator takes that window — same cookies, same page, same scroll position. Nothing is cloned. In
a deployment the operator would attach to the browser's CDP endpoint from their own machine; that
changes who is at the keyboard, not the model beneath.

**Control transfer is enforced, not announced.** The driver holds a `LeaseToken`, checked inside
`act()`. While the human holds it, an automation action raises `LeaseViolation`. Transfers mint a
**fresh token**, so an action queued before the pause cannot fire after it. Verified: stale tokens
and cross-holder tokens are both refused.

**Resume re-establishes position rather than assuming it.** A person handed a stuck flow may clear
the obstacle *or* complete the step themselves. Replay asks the artifact where the screen matches:
blocked step's target resolves → retry it; the *next* step's target resolves instead → the
operator completed it, skip forward; neither → stop. Guessing here is how a flow silently repeats
a write.

**What the operator sees is designed as carefully as the mechanism.** The console words options by
consequence — `approve`/`deny` for an approval hold, `continue`/`stop` when merely stuck — because
"resume" meaning both "carry on" and "authorise this five-figure payment" is how rubber-stamping
starts. It states the case rather than listing values, and computes the comparison that matters:
*"the amount EXCEEDS that balance by 12,659.45 — this will not go through"*. An approval screen
that makes someone do arithmetic under time pressure is one that stops being read.

**Evidence is one timeline.** The operator's turn is a `StepRecord` like any other, in sequence,
with their stated reason carried through into the result.

---

## 6. Safety

**Allowlist and identity.** Three operator roles exercise three genuinely different conditions: a
data-driven denial (business outcome), an identity-driven denial (failure — a provisioning
defect), and session expiry (recoverable). Collapsing those into "permission denied" would let a
mis-provisioned capability fail quietly forever, looking like a stream of legitimate "no"s.

**Risk is value-dependent.** A $500 and a $75,000 transfer are the same verb, same screen, same
recorded step. Classifying by action *type* cannot tell them apart, so rules compare bound input
**values**. Rules also declare whether a limit is absolute (`DENY` — no in-band override, because
a hard limit that bends to whoever is on shift is not a limit) or needs a second pair of eyes
(`REQUIRE_APPROVAL`).

**The guardrail is ours, not the app's.** The transfer screen imposes no limit whatsoever — that
is realistic, and it is the point. Verified: money never moves on a denial, and the ceiling holds
with a willing approver standing by.

**Defence in depth, learned the hard way.** The commit-step gate alone **failed open** for an
artifact recorded before `commit_step_id` existed — a $40,000 deposit went straight through. Hard
ceilings are now also enforced pre-flight, where no artifact field can disable them. A guardrail
that switches itself off when one field is missing is not a guardrail.

**Redaction, and its limits.** Evidence records what the automation *did*, not what the screen
*said* — which is what makes redaction tractable. Values keep shape and lose content
(`$##,###.##`); typed values are recorded as the binding that drove them (`${input.member_id}`),
which is safer *and* more informative. Two real leaks were found by scanning generated evidence
for known sensitive strings: failure detail persisted page text (and so members' names, while the
module docstring directly above it claimed otherwise), and URLs went unredacted (`/member/10234`
identifies a person as surely as a name field). Both are now pinned by tests.

**The honest limit:** pattern matching cannot find a *name*. That is exactly why the defence is
structural — don't persist free text — rather than a scrubber that would quietly fail on the one
field that matters. Residual risk, stated plainly: a control literally labelled with member data
would survive. Screenshots are gitignored because pixels cannot be redacted at all.

**Prompt injection.** Page text is attacker-influenced and aimed at whoever reads it next —
including the human operator. `ContextFragment.source` tags provenance; `summary_line()` is built
only from system-asserted values, so page text is *structurally* unable to reach the headline an
operator reads first. It still reaches them — quoted and labelled untrusted.

---

## 7. Cuts

**Deliberately not built:**

- **Visual locator tier.** In the schema, unimplemented. It only earns its place on canvas-rendered
  or thick-client surfaces; on this one it would be dead code that looked like coverage.
- **A tenant registry / control plane.** Label maps are files loaded by a script. Serving them
  from a registry, versioning them per vendor release, and reconciling them at scale is the
  infrastructure the brief warns against building prematurely — the mechanism is proven with two
  tenants; the plumbing is not the interesting part.
- **A desktop/terminal driver.** The seam is real and the tier-inversion argument is worked out;
  the implementation is a second driver, not a redesign.
- **Semantic/embedding-based locator fallback ("RAG-adjacent").** Considered for the case where a
  tenant renames a control. The structural tier already covers renames when position holds, with
  no model dependency; embeddings would only earn their keep when position changes *too*.
- **A web operator console.** The CLI is a deliberately thin surface. The mechanism underneath —
  lease, live session, evidence — is the part that had to be real.
- **Mid-flow resume after re-authentication.** Recovery re-authenticates, then defers to the
  artifact's `restartable_after_recovery`, defaulting to refusing. Resuming a half-applied write
  safely needs checkpoint reconciliation, which is real work; refusing is the safe default.
- **Per-step risk classes.** Risk is currently evaluated per invocation at the commit step. Per-step
  classes would let the engine reason about *which* steps already committed, which is what
  `restartable_after_recovery` currently answers with a blunt boolean.

**What I'd build next, in order:**

1. **A drift dashboard.** `recorded_tier` mismatches are already collected per run and nothing
   consumes them. Aggregated across tenants this is an early-warning system: it says which
   institutions have drifted while their fallbacks still hold, which is the difference between
   scheduled maintenance and a 2am page.
2. **Checkpoint reconciliation after handoff.** Turns "refuse to resume" into "determine where we
   are and resume safely" — the honest fix for the biggest remaining stop condition.
3. **An agent-facing capability catalog.** Artifacts are already typed and named; exposing them as
   callable tools with typed args is a thin layer over what exists.
4. **A desktop driver**, to prove the seam rather than argue for it.

**Known rough edges:** the mock app holds state in memory, so balances drift across demo runs;
`commit_step_id` is inferred as the last step, which is true for form flows and stated as a
heuristic a reviewer can correct in the artifact.

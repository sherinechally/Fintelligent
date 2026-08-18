"""The SurfaceDriver: perception (screen -> UiSnapshot) and acting
(ControlRef -> a real click/type/select on the live page).

Perception reads the browser's accessibility tree directly via CDP
(`Accessibility.getFullAXTree`) rather than a library convenience method —
see REPORT.md's Architecture section for why. Acting instead uses
Playwright's own locator engine (`get_by_role`, etc.) to actually find and
touch elements — see the note on `resolve()` for why perception and acting
deliberately use two different mechanisms.
"""

from __future__ import annotations

from playwright.sync_api import Browser, Locator, Page, sync_playwright

from computer_use.contracts import (
    Action,
    ActResult,
    ControlRef,
    LocatorTier,
    Resolution,
    ResolutionStatus,
    SemanticRef,
    StructuralRef,
    UiNode,
    UiSnapshot,
    Verb,
)

# Roles the browser's accessibility tree can hand us that we actually care
# about, mapped to our own normalized role vocabulary from contracts.py.
_INTERACTIVE_ROLES = {
    "button": "button",
    "link": "link",
    "textbox": "textbox",
    "combobox": "combobox",
    "checkbox": "checkbox",
}

# The structural tier's ONLY place that knows this app's actual HTML tags —
# see _resolve_structural's docstring for why that knowledge stops here and
# doesn't leak up into StructuralRef.path itself.
_ROLE_TAG_XPATH = {
    "button": "self::button or (self::input and (@type='submit' or @type='button'))",
    "link": "self::a",
    "textbox": "self::input and (@type='text' or not(@type))",
    "combobox": "self::select",
}


def _build_parent_map(nodes: list[dict]) -> dict[str, str]:
    """CDP gives each node's children, not its parent. We need parent lookups
    to tell 'a label sitting on the page' apart from 'the text label Chrome
    generates *inside* a button node' — those are two different things that
    happen to look similar in the raw tree."""
    parent_of: dict[str, str] = {}
    for n in nodes:
        for child_id in n.get("childIds", []):
            parent_of[child_id] = n["nodeId"]
    return parent_of


def _snapshot_from_ax_tree(nodes: list[dict]) -> UiSnapshot:
    node_by_id = {n["nodeId"]: n for n in nodes}
    parent_of = _build_parent_map(nodes)

    ui_nodes: list[UiNode] = []
    for n in nodes:
        if n.get("ignored"):
            continue

        role = n.get("role", {}).get("value")
        name = n.get("name", {}).get("value") or None

        if role in _INTERACTIVE_ROLES:
            value = n.get("value", {}).get("value")
            ui_nodes.append(
                UiNode(
                    node_id=n["nodeId"],
                    role=_INTERACTIVE_ROLES[role],
                    name=name,
                    value=value if value else None,
                )
            )
            continue

        if role == "StaticText" and name:
            # Skip text that's just the label INSIDE a button/link we
            # already captured above — e.g. the button named "Search" has a
            # child StaticText also saying "Search". Keeping both would
            # double every control on the page.
            parent_id = parent_of.get(n["nodeId"])
            parent_role = node_by_id.get(parent_id, {}).get("role", {}).get("value")
            if parent_role in _INTERACTIVE_ROLES:
                continue
            ui_nodes.append(UiNode(node_id=n["nodeId"], role="text", name=name))

    return UiSnapshot(nodes=tuple(ui_nodes))


def _resolve_row_anchor_cell(page: Page, column: str, row_equals: str) -> tuple[Resolution, Locator | None]:
    """"The <column> cell in the row whose some-column says <row_equals>."

    Find the exact cell containing that text, then walk UP to its immediate
    parent row — not the other way around (search all rows, keep the ones
    that "contain" the text somewhere inside them). Searching top-down broke
    on our own nested tables: the page.html has a <table class="panel">
    wrapping the <table class="acctTable">, so the panel's outer row
    "contains" every account row as a descendant, and top-down search
    treated that outer wrapper as a false match. Anchoring on the leaf cell
    itself and walking up one level sidesteps nesting entirely.

    Then find that same table's header row to figure out which position
    `column` is in, and return the cell at that position in the matched
    row. Matching by header TEXT rather than a numeric index is what makes
    this survive a table gaining/losing/reordering columns.

    The anchor search is scoped to DATA CELLS (`td`) specifically, not "any
    element on the page with this text". A page-wide text search is too
    broad in a real app: once this table's filter dropdown is open it holds
    an <option>Savings</option> with identical text, so "the row whose
    Account Type is Savings" started resolving AMBIGUOUS — matching a
    dropdown option that isn't a table row at all. A row anchor is a
    statement about table contents, so it should only ever look at them.
    """
    candidates = page.locator("td")
    # Exact text, not substring: "Savings" must not also match a
    # "Savings Plus" cell, and a cell wrapping a nested table won't have
    # exactly this text either.
    matches = [
        i for i in range(candidates.count())
        if candidates.nth(i).inner_text().strip() == row_equals
    ]
    count = len(matches)
    anchor_cell = candidates.nth(matches[0]) if count == 1 else candidates
    if count == 0:
        return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None
    if count > 1:
        return Resolution(status=ResolutionStatus.AMBIGUOUS, candidate_count=count), None

    row = anchor_cell.locator("xpath=..")
    table = row.locator("xpath=ancestor::table[1]")
    raw_headers = table.locator("tr").first.locator("th, td").all_text_contents()
    # Match on the NORMALIZED, PREFIX of each header, not exact equality —
    # header cells can contain more than just the label (this table's own
    # headers also hold per-column sort/filter icon buttons), so a header's
    # full text is "Account Type <icons...>", not "Account Type" alone.
    header_texts = [" ".join(t.split()) for t in raw_headers]
    col_index = next((i for i, h in enumerate(header_texts) if h.startswith(column)), None)
    if col_index is None:
        return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None
    data_cells = row.locator("td")
    if col_index >= data_cells.count():
        return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None

    return (
        Resolution(status=ResolutionStatus.UNIQUE, tier=LocatorTier.SEMANTIC, candidate_count=1),
        data_cells.nth(col_index),
    )


class PlaywrightDriver:
    """Owns one browser page: perceives it (snapshot) and acts on it (act)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def start(self, headless: bool = True, slow_mo: int = 0) -> None:
        """slow_mo pauses between actions (ms) so a human can watch a headed
        run. Zero in production — this is purely an observation aid."""
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless, slow_mo=slow_mo)
        self._page = self._browser.new_page()
        self._page.goto(self.base_url)

    def goto(self, path: str) -> None:
        assert self._page is not None, "call start() first"
        self._page.goto(self.base_url.rstrip("/") + path)

    def snapshot(self) -> UiSnapshot:
        assert self._page is not None, "call start() first"
        cdp = self._page.context.new_cdp_session(self._page)
        tree = cdp.send("Accessibility.getFullAXTree")
        return _snapshot_from_ax_tree(tree["nodes"])

    def _resolve_semantic(self, sem: SemanticRef) -> tuple[Resolution, Locator | None]:
        assert self._page is not None
        if sem.row_anchor is not None and sem.column is not None:
            return _resolve_row_anchor_cell(self._page, sem.column, sem.row_anchor.equals)

        exact = sem.match == "exact"
        if sem.role == "text":
            # "text" is OUR perception-layer normalization of the browser's
            # StaticText role (see _snapshot_from_ax_tree) — it isn't a real
            # ARIA role, so get_by_role("text", ...) silently matches
            # nothing. get_by_text() is the correct call for it.
            locator = self._page.get_by_text(sem.name, exact=exact) if sem.name else None
        else:
            locator = (
                self._page.get_by_role(sem.role, name=sem.name, exact=exact)
                if sem.name
                else self._page.get_by_role(sem.role)
            )
        if locator is None:
            return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None
        count = locator.count()
        if count == 0:
            return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None
        if count > 1:
            return Resolution(status=ResolutionStatus.AMBIGUOUS, candidate_count=count), None
        return (
            Resolution(status=ResolutionStatus.UNIQUE, tier=LocatorTier.SEMANTIC, candidate_count=1),
            locator,
        )

    def _resolve_structural(
        self, structural: StructuralRef, name: str | None = None, match: str = "exact"
    ) -> tuple[Resolution, Locator | None]:
        """"The NEAREST <role> (optionally named <name>), relative to <anchor>."

        `path` is a small abstract vocabulary — "following:button",
        "following:textbox", etc. — NOT raw XPath or a CSS selector.
        Deliberately kept abstract: UiNode's docstring says "path is opaque,
        driver-interpreted" — if StructuralRef.path held raw DOM tag names
        (input[type=submit] vs button), that knowledge would leak up into
        the discovery/artifact layer, which should only ever think in our
        normalized role vocabulary. Translating "button" into the actual
        HTML this app happens to use is exactly the driver's job.

        `name`, when given, filters candidates by accessible name too — not
        just tag. Without this, three icon-only buttons in a row (e.g. this
        app's per-column sort-ascending / sort-descending / filter icons)
        would be indistinguishable: "the first button after this anchor"
        always means the same one regardless of which icon was intended.
        Filtering by name as well as position is what makes "the FILTER icon
        near Account Type" resolve differently from "the SORT icon near
        Account Type" even though both are "a following button."

        Always resolves to the NEAREST matching element (XPath `[1]`), not
        "the unique one anywhere in the document" — anchoring inherently
        means positional/nearest-match semantics, the same way RowAnchor
        means the nearest row satisfying the anchor, not every row ever.
        Only means something if the anchor itself is unique — an ambiguous
        or missing anchor makes the whole reference meaningless, so we
        propagate its resolution status rather than pretending we found
        something.
        """
        assert self._page is not None
        if structural.anchor is None:
            return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None

        anchor_resolution, anchor_locator = self._resolve_semantic(structural.anchor)
        if anchor_locator is None:
            return anchor_resolution, None

        try:
            direction, role = structural.path.split(":", 1)
        except ValueError:
            return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None
        tag_test = _ROLE_TAG_XPATH.get(role)
        if tag_test is None or direction not in ("following", "preceding"):
            return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None

        predicate = tag_test
        if name:
            # Three different places an accessible name can live: aria-label,
            # text CONTENT (a <button>Search</button>), or the `value`
            # attribute (<input type="submit" value="Search"> has no text
            # content at all — it's a void element, so normalize-space(.) on
            # it is always empty). Missing @value here is exactly the bug
            # that broke the Search-button disambiguation on recovery: our
            # local/global search buttons are both <input type="submit">,
            # not <button>, so only the @value check actually matches them.
            # XPath 1.0 has no case-insensitive contains(), so "contains"
            # match is a plain substring test.
            escaped = name.replace('"', '&quot;')
            if match == "exact":
                predicate = (
                    f'({tag_test}) and (normalize-space(@aria-label)="{escaped}" '
                    f'or normalize-space(.)="{escaped}" or normalize-space(@value)="{escaped}")'
                )
            else:
                predicate = (
                    f'({tag_test}) and (contains(@aria-label, "{escaped}") '
                    f'or contains(., "{escaped}") or contains(@value, "{escaped}"))'
                )

        # Try WITHIN the anchor's own container first, THEN outside it.
        #
        # get_by_text often resolves an anchor to the smallest ENCLOSING
        # element, not a standalone node for the label itself — e.g. "Account
        # Type" is a bare text run directly inside a <th> that ALSO contains
        # that column's own sort/filter icons, so the anchor resolves to the
        # <th>. XPath's `following::` axis explicitly excludes descendants of
        # its context node, so searching `following::` from that <th> skips
        # right past its own icons and finds the NEXT column's instead —
        # silently wrong, not a crash, which is exactly why this needed a
        # real test against a real ambiguous page to catch.
        #
        # `descendant::` covers "the label and its icons share a container"
        # (our column headers). Falling back to `{direction}::` covers "the
        # label and the target are genuinely in different containers" (e.g.
        # "Member ID:" -> the search button in a different table cell).
        locator = anchor_locator.locator(f"xpath=descendant::*[{predicate}][1]")
        if locator.count() == 0:
            locator = anchor_locator.locator(f"xpath={direction}::*[{predicate}][1]")
        count = locator.count()
        if count == 0:
            return Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0), None
        return (
            Resolution(status=ResolutionStatus.UNIQUE, tier=LocatorTier.STRUCTURAL, candidate_count=1),
            locator,
        )

    def resolve(self, ref: ControlRef) -> tuple[Resolution, Locator | None]:
        """Turn a ControlRef into (what happened, a Locator we can act on).

        Deliberately talks to the LIVE page via Playwright's own locator
        engine here, not our CDP-derived UiSnapshot — perception (what does
        the LLM see) and acting (what do we actually click) are allowed to
        use different mechanisms, because they're solving different
        problems: perception needs a clean *description*, acting needs
        something Playwright can actually operate on right now.

        Tries tiers IN ORDER — semantic first, falling back to structural —
        and returns as soon as one resolves UNIQUELY. If semantic comes back
        AMBIGUOUS (e.g. two "Search" buttons on the page — see the target
        app's global nav vs. the page-local search form) and a structural
        fallback is also present on this ControlRef, structural gets a
        chance to disambiguate via its anchor. Visual (tier 3) is designed
        in the schema but not implemented — see REPORT.md Cuts.
        """
        assert self._page is not None, "call start() first"

        last_resolution = Resolution(status=ResolutionStatus.NOT_FOUND, candidate_count=0)
        if ref.semantic is not None:
            resolution, locator = self._resolve_semantic(ref.semantic)
            if resolution.status == ResolutionStatus.UNIQUE:
                return resolution, locator
            last_resolution = resolution

        if ref.structural is not None:
            name = ref.semantic.name if ref.semantic else None
            match = ref.semantic.match if ref.semantic else "exact"
            resolution, locator = self._resolve_structural(ref.structural, name=name, match=match)
            if resolution.status == ResolutionStatus.UNIQUE:
                return resolution, locator
            last_resolution = resolution

        return last_resolution, None

    def act(self, action: Action) -> ActResult:
        """Resolve the action's target and actually carry it out."""
        assert self._page is not None, "call start() first"

        if action.verb == Verb.NAVIGATE:
            self.goto(action.value or "/")
            return ActResult(ok=True, snapshot_after=self.snapshot())

        if action.target is None:
            return ActResult(ok=False, error="no target given for a non-navigate action")

        resolution, locator = self.resolve(action.target)
        if locator is None:
            return ActResult(ok=False, error=f"target not resolved: {resolution.status}")

        read_value: str | None = None
        try:
            if action.verb == Verb.CLICK:
                locator.click()
            elif action.verb == Verb.TYPE:
                locator.fill(action.value or "")
            elif action.verb == Verb.SELECT:
                locator.select_option(label=action.value or "")
            elif action.verb == Verb.READ:
                read_value = locator.inner_text()
        except Exception as e:  # a real click/type failure from the live page
            return ActResult(ok=False, error=str(e))

        return ActResult(ok=True, snapshot_after=self.snapshot(), read_value=read_value)

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

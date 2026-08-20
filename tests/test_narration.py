"""The demo caption must not change what the automation perceives.

If narration leaks into the accessibility tree, the demo is demonstrating
the demo: node counts shift, an ambiguous-target scenario might resolve
differently, and the thing on screen is no longer the thing being tested.

The first implementation put the caption in a shadow root and assumed that
hid it. It does not — shadow DOM encapsulates markup and styles, not
accessibility, and shadow content is exposed to assistive technology by
design. `aria-hidden` is what actually removes a subtree from the
accessibility tree, which is the surface `snapshot()` reads.

Needs the target app on :5000; skipped otherwise so the suite still runs
without it.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from computer_use.drivers.playwright_driver import PlaywrightDriver

BASE = "http://127.0.0.1:5000"


def _app_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/login", timeout=0.5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _app_is_up(), reason="target app not running on :5000"
)


@pytest.fixture
def driver():
    d = PlaywrightDriver(BASE)
    d.start(headless=True)
    try:
        yield d
    finally:
        d.close()


def test_narration_is_invisible_to_the_agent(driver):
    driver.goto("/login")
    before = driver.snapshot()

    driver.narrate("3/10  Closed account", "denial driven by the DATA")
    after = driver.snapshot()

    assert before.nodes == after.nodes, "narration changed what the agent perceives"


def test_narration_is_visible_to_a_human(driver):
    """The other half: a caption nobody can see is not a caption."""
    driver.goto("/login")
    driver.narrate("visible", "to a person watching")
    assert driver._page.evaluate('!!document.getElementById("__demo_banner")')


def test_narration_survives_navigation(driver):
    """The flow navigates constantly; a caption that vanishes on the first
    click would caption nothing."""
    driver.goto("/login")
    driver.narrate("persists", "across page loads")
    driver.goto("/")
    assert driver._page.evaluate('!!document.getElementById("__demo_banner")')
    assert all("persists" not in (n.name or "") for n in driver.snapshot().nodes)

"""What the app does in a real browser, driven by a stubbed model.

Most of this app is frontend -- streaming a document into the iframe's parser, swapping
single regions, letting the page handle its own clicks, spending idle time guessing the
next one. The server tests cannot see any of it. These can, and they run in seconds
because the model is a stub rather than a 3B on the GPU.
"""

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from tests.browser import Watcher, click_text, set_prediction
from tests import browser as browser_helpers
from tests.fake_ollama import FakeOllama

ROOT = Path(__file__).resolve().parent.parent

# Chosen at runtime: a fixed port silently hands the whole suite to whatever dev server
# happens to be running, which looks like a dozen baffling failures rather than a clash.
with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    PORT = _s.getsockname()[1]

# Body content only -- the document, its head and the stylesheet belong to the app.
SHELL = """<style>.tab { cursor: pointer; }</style>
<section data-region="nav">
  <button id="go">Open Results</button>
  <div class="tab" id="styled">Styled Tab</div>
  <a href="https://example.com/real" id="link">A Real Link</a>
</section>
<section data-region="results"><p>original results</p></section>
<section data-region="aside"><p>untouched aside</p></section>
<script>
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('counter')?.addEventListener('click', (e) => {
      e.target.textContent = 'Clicked';
    });
  });
</script>"""

SHELL_WITH_LOCAL = SHELL.replace(
    '<section data-region="aside"><p>untouched aside</p></section>',
    '<section data-region="aside"><button id="counter" data-local>Count</button>'
    '<button id="broken" data-local>Broken Local</button></section>',
)

PATCH = """#plan Show the results
#region results
<p>patched results</p>
#end"""


def free_port_ready(port, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


async def start_app(page, watcher, **kw):
    kw.setdefault("url", f"http://localhost:{PORT}/")
    await browser_helpers.start_app(page, watcher, **kw)


@pytest.fixture(scope="module")
def fake():
    with FakeOllama(chunk_delay=0.004) as f:
        yield f


@pytest.fixture(scope="module")
def server(fake):
    env = {**os.environ, "OLLAMA_URL": fake.url}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(PORT)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert free_port_ready(PORT), "server did not start"
    yield
    proc.terminate()
    proc.wait(timeout=10)


@pytest_asyncio.fixture
async def page(server):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        yield page
        await browser.close()


def scripted(fake, shell=SHELL, patch=PATCH):
    fake.rules.clear()
    fake.fail_with = None
    fake.on("body of a live single-page app", shell)
    fake.default = patch


# --- the first screen -------------------------------------------------------

@pytest.mark.asyncio
async def test_the_concept_becomes_a_screen(fake, page):
    scripted(fake)
    watcher = Watcher(page)
    await start_app(page, watcher)
    assert "original results" in await watcher.text()


@pytest.mark.asyncio
async def test_the_screen_paints_before_the_model_has_finished(fake, page):
    # Streaming into the parser is the difference between a ~0.2s first paint and a
    # 20s one. Assert content is visible while the request is still open.
    scripted(fake)
    watcher = Watcher(page)
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    await set_prediction(page, False)
    await page.fill("#concept", "a test app")
    await page.click("#start-btn")

    painted_while_in_flight = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if watcher.in_flight and (await watcher.text()).strip():
            painted_while_in_flight = True
            break
        if watcher.started and not watcher.in_flight:
            break
        await page.wait_for_timeout(20)
    await watcher.settle()
    assert painted_while_in_flight, "nothing was on screen until the response ended"


@pytest.mark.asyncio
async def test_a_closing_fence_is_not_rendered_as_text(fake, page):
    scripted(fake, shell=f"```html\n{SHELL}\n```\n")
    watcher = Watcher(page)
    await start_app(page, watcher)
    assert "```" not in await watcher.text()


# --- what a click costs -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_click_updates_only_the_region_the_model_named(fake, page):
    scripted(fake)
    watcher = Watcher(page)
    await start_app(page, watcher)

    result = await watcher.turns_taken(lambda: click_text(page, "Open Results"))
    text = await watcher.text()
    assert result["calls"] == 1
    assert "patched results" in text
    assert "untouched aside" in text, "a region nobody named must not change"


@pytest.mark.asyncio
async def test_a_click_on_something_merely_styled_clickable_still_works(fake, page):
    # Models build tabs out of <div class="tab"> with cursor:pointer and no handler.
    # Matching only <button> and <a href> left every one of those permanently dead.
    scripted(fake)
    watcher = Watcher(page)
    await start_app(page, watcher)
    result = await watcher.turns_taken(lambda: click_text(page, "Styled Tab"))
    assert result["calls"] == 1, "a div the page styles as clickable must not be dead"


@pytest.mark.asyncio
async def test_a_generated_link_never_navigates_away(fake, page):
    scripted(fake)
    watcher = Watcher(page)
    await start_app(page, watcher)
    await watcher.turns_taken(lambda: click_text(page, "A Real Link"))
    assert page.url.startswith(f"http://localhost:{PORT}"), "the app navigated away"
    assert "example.com" not in page.frames[1].url


@pytest.mark.asyncio
async def test_a_region_fills_in_as_it_is_written(fake, page):
    # A region used to stay blank until its whole block closed, so a large one was a
    # long blank wait while the model was producing renderable elements the whole time.
    cards = "".join(f"<div class='card'><h2>Card {i}</h2></div>" for i in range(6))
    scripted(fake, patch=f"#plan fill\n#region results\n{cards}\n#end")
    fake.chunk_delay = 0.03
    watcher = Watcher(page)
    await start_app(page, watcher)

    await click_text(page, "Open Results")
    seen_partial = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        showing = (await watcher.text()).count("Card ")
        if 0 < showing < 6:
            seen_partial = True
            break
        if showing == 6:
            break
        await page.wait_for_timeout(15)
    await watcher.settle()
    fake.chunk_delay = 0.004
    assert seen_partial, "the region went from empty to complete with nothing in between"
    assert (await watcher.text()).count("Card ") == 6


# --- interactions that should cost nothing ----------------------------------

@pytest.mark.asyncio
async def test_a_control_the_page_handles_itself_costs_no_model_call(fake, page):
    scripted(fake, shell=SHELL_WITH_LOCAL)
    watcher = Watcher(page)
    await start_app(page, watcher)
    result = await watcher.turns_taken(lambda: click_text(page, "Count"))
    assert result["calls"] == 0, "a working local control must not reach the model"
    assert "Clicked" in await watcher.text()


@pytest.mark.asyncio
async def test_a_local_control_that_does_nothing_is_not_a_dead_end(fake, page):
    # Models mark controls data-local and then write no handler for them. Treating the
    # attribute as a promise made those clicks dead forever.
    scripted(fake, shell=SHELL_WITH_LOCAL)
    watcher = Watcher(page)
    await start_app(page, watcher)
    result = await watcher.turns_taken(lambda: click_text(page, "Broken Local"))
    assert result["calls"] == 1, "a local control with no handler must fall back to a turn"


@pytest.mark.asyncio
async def test_a_form_the_page_does_not_really_handle_still_submits(fake, page):
    # The same failure as a dead button and worse to live with: a search box that
    # swallows every query in silence. The click path had a grace period for this; the
    # submit path returned early with none.
    shell = SHELL.replace(
        '<section data-region="aside"><p>untouched aside</p></section>',
        '<section data-region="aside"><form data-local>'
        '<input name="q" type="search"><button type="submit">Search</button></form></section>',
    )
    scripted(fake, shell=shell)
    watcher = Watcher(page)
    await start_app(page, watcher)
    result = await watcher.turns_taken(lambda: click_text(page, "Search"))
    assert result["calls"] == 1, "a form with no working handler must fall back to a turn"


# --- guessing ahead ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_guessed_click_is_far_faster_than_an_unguessed_one(fake, page):
    # Measured against a cold click in the same run rather than a fixed threshold, so it
    # states what it means -- guessing ahead pays off -- on any machine. It compares how
    # long the screen took to finish changing: regions stream in now, so a cold click
    # also shows something almost immediately and first-change no longer separates them.
    counter = iter(range(100))
    scripted(fake, patch=lambda _: "#plan turn\n#region results\n"
             + f"<p>turn {next(counter)}</p>" + "<p class='muted'>filler</p>" * 20 + "\n#end")
    fake.chunk_delay = 0.02

    watcher = Watcher(page)
    await start_app(page, watcher, predict=False)
    cold = (await watcher.time_to_stable(lambda: click_text(page, "Styled Tab")))["seconds"]

    # Guessing only runs after a turn, so enabling it needs a turn to follow.
    await set_prediction(page, True)
    guesses_before = watcher.started
    await watcher.turns_taken(lambda: click_text(page, "Styled Tab"))
    await watcher.settle(quiet=2.5)
    assert watcher.started > guesses_before + 1, "idle time was not spent guessing"

    warm = (await watcher.time_to_stable(lambda: click_text(page, "Open Results")))["seconds"]
    assert warm < cold / 3, f"guessing saved nothing: {warm:.3f}s vs cold {cold:.3f}s"
    fake.chunk_delay = 0.004


# Idle guessing only reaches the first few controls, so a page needs more than that
# before hovering can show it is doing anything the idle pass was not already doing.
CROWDED_SHELL = (
    '<section data-region="nav">'
    + "".join(f'<button id="b{i}">Choice {i}</button>' for i in range(6))
    + "</section><section data-region=\"results\"><p>original results</p></section>"
)


@pytest.mark.asyncio
async def test_pointing_at_a_control_gets_a_head_start_on_clicking_it(fake, page):
    # Hover arrives a few hundred milliseconds before the click and says far more about
    # what the user wants than working through controls in page order does.
    counter = iter(range(100))
    scripted(fake, shell=CROWDED_SHELL,
             patch=lambda _: "#plan turn\n#region results\n"
             + f"<p>turn {next(counter)}</p>" + "<p class='muted'>filler</p>" * 60 + "\n#end")
    fake.chunk_delay = 0.02

    watcher = Watcher(page)
    await start_app(page, watcher, predict=True)
    await watcher.settle(quiet=1.5)          # idle guessing covers the first few

    # "Choice 5" is past where the idle pass reaches, so this is a genuine cold turn.
    cold = (await watcher.time_to_stable(lambda: click_text(page, "Choice 5")))["seconds"]
    await watcher.settle(quiet=1.5)

    await (await page.frames[1].query_selector('text="Choice 4"')).hover()
    await watcher.settle(quiet=1.5)          # let the hover guess finish
    hovered = (await watcher.time_to_stable(lambda: click_text(page, "Choice 4")))["seconds"]

    fake.chunk_delay = 0.004
    # Halved rather than thirded: both numbers are small against a stub, and the margin
    # narrows when the suite runs the browser under load. The claim is that hovering
    # changes the order of magnitude of the wait, not that it hits an exact ratio.
    assert hovered < cold / 2, f"hovering bought nothing: {hovered:.3f}s vs cold {cold:.3f}s"


@pytest.mark.asyncio
async def test_repeated_region_ids_are_made_unique(fake, page):
    # An id addresses a region, so a repeated one means every patch aimed at it lands on
    # the first copy and the rest can never be updated at all.
    duplicated = (
        "".join(f'<section data-region="item"><h2>Item {i}</h2></section>' for i in range(4))
        + '<section data-region="nav"><button id="go">Open Results</button></section>'
    )
    scripted(fake, shell=duplicated)
    watcher = Watcher(page)
    await start_app(page, watcher)
    await watcher.turns_taken(lambda: click_text(page, "Open Results"))

    prompt = "\n".join(m["content"] for m in fake.requests[-1]["messages"])
    ids = re.findall(r'data-region="([^"]+)"', prompt)
    assert len(ids) == len(set(ids)), f"the model was shown duplicate region ids: {ids}"


@pytest.mark.asyncio
async def test_guessing_is_not_cancelled_by_the_page_changing_under_the_cursor(fake, page):
    # Every turn replaces a region, and replacing the DOM under a cursor that has not
    # moved fires a hover event. Treating that as intent cancelled the idle pass moments
    # after it began, leaving one guess where there should have been three.
    scripted(fake, shell=CROWDED_SHELL)
    watcher = Watcher(page)
    await start_app(page, watcher, predict=True)
    await watcher.settle(quiet=1.5)

    # Click, leaving the pointer resting where the click landed. turns_taken settles, so
    # the guessing it triggers is already under way by the time it returns -- count from
    # before the click and expect the turn plus a full idle pass.
    before = watcher.started
    await watcher.turns_taken(lambda: click_text(page, "Choice 0"))
    await watcher.settle(quiet=2.0)
    # A full idle pass covers three controls. The click itself may cost nothing, since
    # the pass before it may already have guessed this one.
    assert watcher.started - before >= 3, (
        "idle guessing stopped early: the page moving under a still cursor cancelled it"
    )


# --- the failure that destroys work ----------------------------------------

@pytest.mark.asyncio
async def test_a_bad_screen_swap_cannot_blank_the_app(fake, page):
    scripted(fake, patch="#screen\n<h2>About</h2>\n#end")
    watcher = Watcher(page)
    await start_app(page, watcher)
    await watcher.turns_taken(lambda: click_text(page, "Open Results"))
    text = await watcher.text()
    assert "About" in text
    assert "Open Results" in text, "the whole page was replaced by one region's content"


@pytest.mark.asyncio
async def test_a_model_failure_is_visible_rather_than_silent(fake, page):
    scripted(fake)
    watcher = Watcher(page)
    await start_app(page, watcher)
    fake.fail_with = "model 'nope' not found"
    await watcher.turns_taken(lambda: click_text(page, "Open Results"))
    assert "nope" in await watcher.text(), "the failure left no trace on screen"

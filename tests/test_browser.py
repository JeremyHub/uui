"""What the app does in a real browser, driven by a stubbed model.

Most of this app is frontend -- streaming a document into the iframe's parser, swapping
single regions, letting the page handle its own clicks, spending idle time guessing the
next one. The server tests cannot see any of it. These can, and they run in seconds
because the model is a stub rather than a 3B on the GPU.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from tests.browser import Watcher, click_text, set_prediction, start_app
from tests.fake_ollama import FakeOllama

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765

SHELL = """<!DOCTYPE html><html><head><style>
  body { margin: 0; font-family: sans-serif; }
  .tab { cursor: pointer; padding: 8px; }
  .plain { padding: 8px; }
</style></head><body>
<section data-region="nav">
  <button id="go">Open Results</button>
  <div class="tab" id="styled">Styled Tab</div>
  <a href="https://example.com/real" id="link">A Real Link</a>
</section>
<section data-region="results"><p>original results</p></section>
<section data-region="aside"><p>untouched aside</p></section>
<script>
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('counter').addEventListener('click', (e) => {
      e.target.textContent = 'Clicked';
    });
  });
</script>
</body></html>"""

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
    fake.on("COMPLETE HTML DOCUMENT", shell)
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


# --- guessing ahead ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_guessed_click_is_far_faster_than_an_unguessed_one(fake, page):
    # Compared against a cold click in the same run rather than a fixed threshold, so it
    # states what it means -- guessing ahead pays off -- on any machine, without needing
    # to know that a prediction cache exists.
    counter = iter(range(100))
    scripted(fake, patch=lambda _: f"#plan turn\n#region results\n<p>turn {next(counter)}</p>\n#end")
    fake.chunk_delay = 0.02      # a turn the stub cannot finish before it is noticed

    watcher = Watcher(page)
    await start_app(page, watcher, predict=False)
    cold = await watcher.time_to_change(lambda: click_text(page, "Styled Tab"))
    await watcher.settle()

    # Guessing only runs after a turn, so enabling it needs a turn to follow.
    await set_prediction(page, True)
    guesses_before = watcher.started
    await watcher.turns_taken(lambda: click_text(page, "Styled Tab"))
    await watcher.settle(quiet=2.5)
    assert watcher.started > guesses_before + 1, "idle time was not spent guessing"

    # Timing is the whole claim, and the only signal that stays honest: a click that
    # aborts an in-flight guess still briefly shows a request outstanding, so counting
    # requests would measure the abort rather than the hit.
    warm = await watcher.time_to_change(lambda: click_text(page, "Open Results"))
    assert warm["seconds"] < cold["seconds"] / 3, (
        f"guessing saved nothing: {warm['seconds']:.3f}s vs cold {cold['seconds']:.3f}s"
    )
    fake.chunk_delay = 0.004


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

"""What the app does in a real browser, driven by a stubbed model.

Most of this app is frontend -- streaming a document into the iframe's parser, swapping
single regions, letting the page handle its own clicks, spending idle time guessing the
next one. The server tests cannot see any of it. These can, and they run in seconds
because the model is a stub rather than a 3B on the GPU.
"""

import asyncio
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


# --- remembering the session ------------------------------------------------

FORM_SHELL = (
    '<section data-region="nav">'
    + "".join(f'<button id="b{i}">Choice {i}</button>' for i in range(4))
    + "</section>"
    '<section data-region="entry"><form><input name="nickname" type="text">'
    '<button type="submit">Save</button></form></section>'
    '<section data-region="results"><p>original results</p></section>'
)


def prompts_sent(fake):
    return ["\n".join(m["content"] for m in r["messages"]) for r in fake.requests]


async def wait_for_prompt(fake, needle, limit=25):
    """Wait until the model has been sent a prompt containing `needle`.

    Settling is not enough when a turn goes out to an API: that request does not go to
    the model endpoint, so the watcher sees nothing in flight and the screen is not
    changing either. What is being waited for here is the second ask, so wait for it.
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        hit = [p for p in prompts_sent(fake) if needle in p]
        if hit:
            return hit[-1]
        await asyncio.sleep(0.1)
    raise AssertionError(f"no prompt containing {needle!r} was ever sent")


@pytest.mark.asyncio
async def test_what_the_user_typed_survives_later_turns(fake, page):
    # A turn sees the screen and the click that caused it. Anything the user established
    # earlier is no longer on screen, so without a record the app starts contradicting
    # itself exactly when a session gets long enough to be worth having.
    scripted(fake, shell=FORM_SHELL)
    watcher = Watcher(page)
    await start_app(page, watcher)

    await page.frames[1].fill('input[name="nickname"]', "Wintermute")
    await watcher.turns_taken(lambda: click_text(page, "Save"))
    for i in range(3):
        await watcher.turns_taken(lambda i=i: click_text(page, f"Choice {i}"))

    assert "Wintermute" in prompts_sent(fake)[-1], (
        "three turns later the app no longer knows what the user entered"
    )


@pytest.mark.asyncio
async def test_a_long_session_does_not_grow_the_prompt_without_bound(fake, page):
    # Keeping every turn verbatim is the obvious way to stay consistent and the wrong
    # one: prompts grow forever, and on a local model that is paid for on every turn.
    fake.rules.clear()
    fake.fail_with = None
    fake.on("body of a live single-page app", FORM_SHELL)
    fake.on("keep the running memory", "The user has been clicking through the choices.")
    fake.default = PATCH

    watcher = Watcher(page)
    await start_app(page, watcher)
    for i in range(14):
        await watcher.turns_taken(lambda i=i: click_text(page, f"Choice {i % 4}"))
    await watcher.settle(quiet=1.5)

    assert any("keep the running memory" in p for p in prompts_sent(fake)), (
        "the session was never compacted, so the prompt only ever grows"
    )
    patch_prompts = [p for p in prompts_sent(fake) if "SCREEN:" in p]
    early, late = patch_prompts[1], patch_prompts[-1]
    assert late.count("RECENTLY") <= 1
    assert len(late) < len(early) * 2, (
        f"prompt grew from {len(early)} to {len(late)} chars over a long session"
    )


# --- where the model runs ---------------------------------------------------

NO_ADAPTER = """
  if (navigator.gpu) {
    Object.defineProperty(navigator, 'gpu', { value: { requestAdapter: async () => null } });
  }
"""


@pytest.mark.asyncio
async def test_in_tab_inference_is_not_offered_without_a_working_adapter(fake, page):
    # "gpu" in navigator is true on machines where requestAdapter then returns null.
    # Offering in-tab inference there means a user picks it and waits for a
    # multi-gigabyte download that cannot work.
    await page.add_init_script(NO_ADAPTER)
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    await page.wait_for_function("document.getElementById('engine').options.length > 0")
    engines = await page.evaluate("[...document.getElementById('engine').options].map(o => o.value)")
    assert engines == ["server"]


@pytest.mark.asyncio
async def test_the_picker_takes_the_largest_model_that_fits(fake, page):
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    picked = await page.evaluate("""(async () => {
      const m = await import('./engine.js');
      const models = [
        { id: 'tiny-Instruct', sizeMB: 400, lowResource: true },
        { id: 'mid-Instruct', sizeMB: 1600, lowResource: false },
        { id: 'big-Instruct', sizeMB: 3000, lowResource: false },
        { id: 'huge-Instruct', sizeMB: 9000, lowResource: false },
      ];
      return {
        roomy: m.pickModel(models, 4000).id,
        tight: m.pickModel(models, 1800).id,
        none: m.pickModel(models, 100).id,
      };
    })()""")
    assert picked["roomy"] == "big-Instruct", "left capacity on the table"
    assert picked["tight"] == "mid-Instruct", "picked something that would not fit"
    # Nothing fits: better to try the smallest than to refuse outright.
    assert picked["none"] == "tiny-Instruct"


@pytest.mark.asyncio
async def test_the_picker_avoids_models_too_small_to_follow_the_format(fake, page):
    # The protocol asks for structured output with #region markers. Below roughly a
    # billion parameters a model cannot hold to it, so the app has nothing to apply --
    # a model that fits comfortably but cannot answer is not the better choice.
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    picked = await page.evaluate("""(async () => {
      const m = await import('./engine.js');
      return m.pickModel([
        { id: 'tiny-Instruct', sizeMB: 360, lowResource: true },
        { id: 'small-Instruct', sizeMB: 580, lowResource: true },
        { id: 'proper-Instruct', sizeMB: 1900, lowResource: false },
      ], 4000).id;
    })()""")
    assert picked == "proper-Instruct"


@pytest.mark.asyncio
async def test_the_budget_is_not_inflated_past_what_the_card_holds(fake, page):
    # maxBufferSize is a per-buffer cap, not a VRAM figure, but it lands close on real
    # hardware: a 4GB RX 570 reports 4GB. An earlier version scaled it up, which turned
    # that card into a 16GB budget -- a model that downloads for minutes and then fails
    # to allocate. Too small is a working app with a weaker model; too large is a long
    # wait ending in nothing.
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    budgets = await page.evaluate("""(async () => {
      const m = await import('./engine.js');
      const GB = 1024 * 1024 * 1024;
      return {
        card4gb: m.budgetFromLimits({ maxBufferSize: 4 * GB, maxStorageBufferBindingSize: 4 * GB }),
        software: m.budgetFromLimits({ maxBufferSize: 1 * GB, maxStorageBufferBindingSize: 1 * GB }),
        absurd: m.budgetFromLimits({ maxBufferSize: 64 * GB }),
        missing: m.budgetFromLimits({}),
      };
    })()""")
    assert budgets["card4gb"] == 4096
    assert budgets["software"] == 1024
    assert budgets["absurd"] <= 8192, "an implausible limit was taken at face value"
    assert 0 < budgets["missing"] <= 4096, "no limits reported should mean a timid guess"


@pytest.mark.asyncio
async def test_models_this_device_cannot_start_are_not_offered(fake, page):
    # Half the prebuilt models are f16 quantised and refuse to start without the
    # shader-f16 extension. Offering one means the refusal arrives after a
    # gigabyte-scale download -- the most expensive way possible to find out.
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    listed = await page.evaluate("""(async () => {
      const m = await import('./engine.js');
      const entries = [
        { model_id: 'A-Instruct-q4f16_1-MLC', vram_required_MB: 900 },
        { model_id: 'A-Instruct-q4f32_1-MLC', vram_required_MB: 1100 },
        { model_id: 'SomeBase-Model-q4f32_1-MLC', vram_required_MB: 800 },
      ];
      return {
        withF16: m.usableModels(entries, { f16: true }).map(x => x.id),
        withoutF16: m.usableModels(entries, { f16: false }).map(x => x.id),
      };
    })()""")
    assert "A-Instruct-q4f16_1-MLC" in listed["withF16"]
    assert "A-Instruct-q4f16_1-MLC" not in listed["withoutF16"]
    # A base model cannot follow the reply format at all, so it is never offered.
    assert not any("SomeBase" in m for m in listed["withF16"])


@pytest.mark.asyncio
async def test_a_coder_model_is_preferred_at_the_same_size(fake, page):
    # The whole output is markup, and a coder-tuned model of the same size holds the
    # reply format much better.
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    picked = await page.evaluate("""(async () => {
      const m = await import('./engine.js');
      return m.pickModel([
        { id: 'Generic-3B-Instruct', sizeMB: 2000, lowResource: false },
        { id: 'Qwen2.5-Coder-3B-Instruct', sizeMB: 1900, lowResource: false },
      ], 4000).id;
    })()""")
    assert "Coder" in picked


# --- the loading overlay ----------------------------------------------------

async def overlay_visible(page):
    return await page.evaluate("!document.getElementById('loading').hidden")


@pytest.mark.asyncio
async def test_the_screen_is_covered_while_the_first_one_is_built(fake, page):
    scripted(fake)
    fake.chunk_delay = 0.02
    watcher = Watcher(page)
    await page.goto(f"http://localhost:{PORT}/", wait_until="load")
    await set_prediction(page, False)
    await page.fill("#concept", "a test app")
    await page.click("#start-btn")

    seen = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if await overlay_visible(page):
            seen = True
            break
        await page.wait_for_timeout(20)
    await watcher.settle()
    fake.chunk_delay = 0.004
    assert seen, "the first screen was built with nothing to say so"
    assert not await overlay_visible(page), "the overlay outlived the turn"


@pytest.mark.asyncio
async def test_a_click_during_a_turn_does_not_reach_the_page(fake, page):
    # Mid-turn the page is half-rewritten: a region emptied, controls about to be
    # replaced. Clicks were ignored anyway -- the turn in flight wins -- but looked like
    # they should work, which is worse than being told to wait.
    # Long enough that the turn outlives the overlay's grace period; a short turn is
    # supposed to finish without ever showing it.
    scripted(fake, patch="#plan slow\n#region results\n"
             + "<p class='muted'>filler</p>" * 40 + "\n#end")
    fake.chunk_delay = 0.02
    watcher = Watcher(page)
    await start_app(page, watcher)

    await click_text(page, "Open Results")
    await page.wait_for_timeout(400)
    assert await overlay_visible(page), "nothing was covering the page mid-turn"

    calls_before = watcher.started
    # A click aimed at where a control is; the overlay is on top, so it lands there.
    box = await (await page.frames[1].query_selector('text="Styled Tab"')).bounding_box()
    stage = await page.query_selector("#stage")
    origin = await stage.bounding_box()
    await page.mouse.click(origin["x"] + box["x"] + box["width"] / 2,
                           origin["y"] + box["y"] + box["height"] / 2)
    await page.wait_for_timeout(300)
    assert watcher.started == calls_before, "a click got through to the page mid-turn"

    await watcher.settle()
    fake.chunk_delay = 0.004
    assert not await overlay_visible(page)


@pytest.mark.asyncio
async def test_an_instant_turn_never_flashes_the_overlay(fake, page):
    # A guessed click applies in milliseconds. Flashing a progress card over it would
    # make the fastest thing the app does look like the slowest.
    scripted(fake)
    watcher = Watcher(page)
    await start_app(page, watcher, predict=True)
    await watcher.settle(quiet=1.5)

    await click_text(page, "Open Results")
    flashed = False
    for _ in range(8):
        if await overlay_visible(page):
            flashed = True
            break
        await page.wait_for_timeout(20)
    await watcher.settle(quiet=1.0)
    assert not flashed, "an instant update still showed a loading overlay"


# --- live data --------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_model_that_asks_for_data_gets_it_and_is_asked_again(fake, page, monkeypatch):
    # A generated page can show real information, but only if it says it needs some: the
    # round trip is only worth paying for when the model cannot answer without it.
    asked = {"count": 0}

    def reply(_prompt):
        asked["count"] += 1
        # The shell is answered by a rule, so this is the first patch attempt.
        if asked["count"] == 1:
            return "#fetch https://api.tvmaze.com/search/shows?q=dune"
        return "#plan show it\n#region results\n<p>Dune (2021)</p>\n#end"

    scripted(fake)
    fake.default = reply

    watcher = Watcher(page)
    await start_app(page, watcher)
    await watcher.turns_taken(lambda: click_text(page, "Open Results"))

    await wait_for_prompt(fake, "DATA FROM https://api.tvmaze.com")
    await watcher.settle(quiet=1.0)
    assert "Dune (2021)" in await watcher.text()


@pytest.mark.asyncio
async def test_data_that_could_not_be_had_is_reported_to_the_model_not_hidden(fake, page):
    # Told the data is unavailable the model writes a page that says so. Given silence
    # it invents the numbers, which is the one outcome worth going out of the way to
    # avoid for something presented as real data.
    asked = {"count": 0}

    def reply(_prompt):
        asked["count"] += 1
        if asked["count"] == 1:
            # Refused without touching the network, so the suite stays offline.
            return "#fetch file:///etc/passwd"
        return "#plan say so\n#region results\n<p>no data</p>\n#end"

    scripted(fake)
    fake.default = reply

    watcher = Watcher(page)
    await start_app(page, watcher)
    await watcher.turns_taken(lambda: click_text(page, "Open Results"))

    followup = await wait_for_prompt(fake, "unavailable")
    assert "Only http and https URLs can be fetched" in followup


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

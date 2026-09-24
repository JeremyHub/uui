"""The turn protocol itself: compaction, the reply parser, and the guards around them.

These moved from Python to JavaScript when the app learned to run without a server, so
they are exercised in a browser -- which is also the honest place for them, since the
compaction now walks a real DOM rather than parsing HTML text.

tests/test_browser.py covers what the app does; this covers what these functions do,
including the cases a real model produces only occasionally and a test can produce on
demand.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent

with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    PORT = _s.getsockname()[1]


def port_ready(port, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="module")
def server():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(PORT)],
        cwd=ROOT, env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert port_ready(PORT), "server did not start"
    yield
    proc.terminate()
    proc.wait(timeout=10)


@pytest_asyncio.fixture
async def page(server):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(f"http://localhost:{PORT}/", wait_until="load")
        yield page
        await browser.close()


async def run(page, body, arg=None):
    """Evaluate `body` with the protocol modules in scope."""
    return await page.evaluate(
        """async (arg) => {
            const screen = await import('./screen.js');
            const parser = await import('./parser.js');
            const turn = await import('./turn.js');
            const scratch = document.createElement('div');
            document.body.append(scratch);
            try { return await (%s); } finally { scratch.remove(); }
        }""" % body,
        arg,
    )


# --- compacting the screen ---------------------------------------------------

async def compact(page, html, keep=None):
    return await run(page, """(() => {
        scratch.innerHTML = arg.html;
        return arg.keep === null ? screen.compact(scratch) : screen.compact(scratch, arg.keep);
    })()""", {"html": html, "keep": keep})


def repeated(n, cls="row"):
    return "<ul>" + "".join(f'<li class="{cls}">Item {i}</li>' for i in range(n)) + "</ul>"


@pytest.mark.asyncio
async def test_short_runs_are_sent_verbatim(page):
    # Eliding a three-item list saves almost nothing and risks the model copying the
    # marker into its answer, so short runs stay whole.
    out = await compact(page, repeated(3))
    assert out.count('class="row"') == 3
    assert "more" not in out


@pytest.mark.asyncio
async def test_long_runs_collapse_to_a_pattern_plus_a_count(page):
    out = await compact(page, repeated(9))
    assert out.count('class="row"') == 4
    assert "5 more" in out


@pytest.mark.asyncio
async def test_runs_collapse_at_the_top_level_of_a_region_too(page):
    # A region is usually a flat list of cards with no wrapper, which is exactly the
    # case an earlier version missed entirely.
    cards = "".join(f'<div class="card"><h2>Card {i}</h2></div>' for i in range(8))
    out = await compact(page, cards)
    assert out.count('class="card"') == 4
    assert "4 more" in out


@pytest.mark.asyncio
async def test_different_kinds_of_sibling_do_not_collapse_together(page):
    mixed = "".join(f'<div class="a">{i}</div><div class="b">{i}</div>' for i in range(6))
    assert "more" not in await compact(page, mixed)


@pytest.mark.asyncio
async def test_class_names_and_structure_survive(page):
    # The whole reason for sending markup instead of a summary: without classes the
    # model rewrites styled cards as bare unstyled divs.
    out = await compact(page, '<div class="card"><img src="https://x/a.jpg" alt="A"><h2>Hi</h2></div>')
    assert 'class="card"' in out
    assert "<img" in out and 'alt="A"' in out


@pytest.mark.asyncio
async def test_long_text_is_truncated_but_the_element_survives(page):
    out = await compact(page, "<p>%s</p>" % ("word " * 200))
    assert out.startswith("<p>") and out.endswith("</p>")
    assert len(out) < 400


@pytest.mark.asyncio
async def test_a_nested_region_is_not_repeated_inside_its_parent(page):
    # Every region is sent separately. Serialising a child into its parent as well puts
    # the same content in the prompt twice and invites a patch that clobbers the child.
    out = await compact(
        page,
        '<p>own content</p><section data-region="inner"><p>child content</p></section>',
    )
    assert "own content" in out
    assert "child content" not in out
    assert "inner" in out, "the child should still be mentioned, just not repeated"


@pytest.mark.asyncio
async def test_render_screen_labels_every_region_and_respects_a_budget(page):
    result = await run(page, """(() => {
        const doc = document.implementation.createHTMLDocument('t');
        doc.body.innerHTML = arg.html;
        return { small: screen.renderScreen(doc), capped: screen.renderScreen(doc, 400) };
    })()""", {"html": "".join(
        f'<section data-region="r{i}"><p>{"x" * 500}</p></section>' for i in range(4)
    )})
    assert all(f'data-region="r{i}"' in result["small"] for i in range(4))
    assert len(result["capped"]) <= 500


# --- parsing a reply ---------------------------------------------------------

async def parse(page, text, pieces=24):
    """Feed `text` through the parser in small pieces, the way a model streams it."""
    return await run(page, """(() => {
        const p = new parser.PatchParser();
        const events = [];
        const chunks = arg.text.match(new RegExp('[\\\\s\\\\S]{1,' + arg.pieces + '}', 'g')) || [];
        for (const c of chunks) events.push(...p.feed(c));
        events.push(...p.finish());
        return events;
    })()""", {"text": text, "pieces": pieces})


PATCH = "#plan Show the results\n#region results\n<p>patched results</p>\n#end"


@pytest.mark.asyncio
async def test_a_patch_returns_only_the_named_region(page):
    events = await parse(page, PATCH)
    assert [e["id"] for e in events if e["type"] == "region"] == ["results"]
    assert [e["text"] for e in events if e["type"] == "plan"] == ["Show the results"]


@pytest.mark.asyncio
async def test_regions_are_emitted_as_they_finish_not_at_the_end(page):
    # Region order and arrival are part of the contract: each one reaches the screen
    # while the model is still writing the next.
    events = await parse(page, "#plan two\n#region one\n<p>1</p>\n#region two\n<p>2</p>\n#end")
    kinds = [(e["type"], e.get("id")) for e in events if e["type"] in ("plan", "region_open", "region")]
    assert kinds == [
        ("plan", None), ("region_open", "one"), ("region", "one"),
        ("region_open", "two"), ("region", "two"),
    ]


@pytest.mark.asyncio
async def test_a_region_written_on_one_line_still_arrives_in_pieces(page):
    # Models do not reliably put newlines between elements. Waiting for one hands the
    # whole block over at once, which is the blank wait chunking exists to remove.
    cards = "".join(f"<div class='card'><h2>C{i}</h2></div>" for i in range(5))
    events = await parse(page, f"#plan fill\n#region results\n{cards}\n#end")
    chunks = [e for e in events if e["type"] == "region_chunk"]
    assert len(chunks) == 5, "the region arrived in one piece"
    assert all(c["html"].startswith("<div") and c["html"].endswith("</div>") for c in chunks), (
        "a chunk was cut mid-element and would not render on its own"
    )


@pytest.mark.asyncio
async def test_a_final_region_survives_a_missing_end_marker(page):
    events = await parse(page, "#plan truncated\n#region results\n<p>content</p>")
    assert [e["id"] for e in events if e["type"] == "region"] == ["results"]


@pytest.mark.asyncio
async def test_compaction_markers_never_reach_the_page(page):
    # Small models treat the elided screen as a template and echo the marker straight
    # back, which would render as a stray comment where content should be.
    events = await parse(page, "#plan copy\n#region results\n<p>a</p>\n"
                               "<!-- and 5 more <li> like those above: WRITE THEM ALL OUT IN FULL -->\n#end")
    html = next(e["html"] for e in events if e["type"] == "region")
    assert "more" not in html and "<p>a</p>" in html


@pytest.mark.asyncio
async def test_fences_inside_a_patch_are_ignored(page):
    events = await parse(page, "#plan fenced\n#region results\n```html\n<p>hi</p>\n```\n#end")
    assert next(e["html"] for e in events if e["type"] == "region") == "<p>hi</p>"


@pytest.mark.asyncio
async def test_a_body_wrapper_is_stripped_from_a_screen_swap(page):
    events = await parse(page, '#screen\n<body><section data-region="a">new</section></body>\n#end')
    html = next(e["html"] for e in events if e["type"] == "screen")
    assert not html.lower().startswith("<body")


# --- guards ------------------------------------------------------------------

async def guard(page, event_html, clicked_in=None):
    return await run(page, """(() => {
        const doc = document.implementation.createHTMLDocument('t');
        doc.body.innerHTML =
          '<section data-region="nav">' + 'n'.repeat(400) + '</section>' +
          '<section data-region="main">' + 'm'.repeat(200) + '</section>';
        return turn.guardScreen(
          { type: 'screen', html: arg.html },
          { doc, action: { elementData: { inRegion: arg.clickedIn } } },
        );
    })()""", {"html": event_html, "clickedIn": clicked_in})


@pytest.mark.asyncio
async def test_a_screen_swap_without_regions_cannot_wipe_the_page(page):
    # A #screen replaces all the body content, so trusting a reply with no regions in it
    # blanks the app. It is a region's worth of content the model mislabelled.
    result = await guard(page, "<h2>About</h2><p>hello</p>")
    assert result["type"] == "region"


@pytest.mark.asyncio
async def test_demoted_content_does_not_land_on_the_clicked_control(page):
    # The biggest region is usually the navigation. Dropping an About page into the nav
    # deletes every way of getting anywhere -- worse than the blank screen this prevents.
    assert (await guard(page, "<h2>About</h2>"))["id"] == "nav"
    assert (await guard(page, "<h2>About</h2>", clicked_in="nav"))["id"] == "main"


@pytest.mark.asyncio
async def test_a_real_screen_swap_is_left_alone(page):
    result = await guard(page, '<section data-region="a">new</section>')
    assert result["type"] == "screen"


# --- reading the model's opening ---------------------------------------------

@pytest.mark.asyncio
async def test_body_content_start_finds_the_content(page):
    cases = await run(page, """({
        plain: turn.bodyContentStart('<section data-region="a">hi</section>'),
        preamble: turn.bodyContentStart('Sure! Here it is:\\n<section data-region="a">hi</section>'),
        document: turn.bodyContentStart('<!DOCTYPE html><html><head><title>x</title></head><body><section>hi</section>'),
        undecided: turn.bodyContentStart('Let me think about'),
    })""")
    assert cases["plain"] == 0
    assert cases["preamble"] > 0
    # A whole document must not land inside the body the app has already opened.
    assert cases["document"] > 0
    # Still ambiguous: better to wait than to write commentary into the page.
    assert cases["undecided"] == -1


@pytest.mark.asyncio
async def test_the_patch_prompt_carries_the_markup_the_model_has_to_match(page):
    message = await run(page, """(() => {
        const doc = document.implementation.createHTMLDocument('t');
        doc.body.innerHTML = '<section data-region="grid"><div class="card"><h2>Siamese</h2></div></section>';
        return turn.buildPatchMessage({
          concept: 'a cat gallery', doc, memory: 'STORY SO FAR:\\nthe user likes cats',
          action: { event: 'click', elementData: { text: 'Next' } },
        });
    })()""")
    assert 'class="card"' in message, "the patch must see the markup it has to match"
    assert "a cat gallery" in message
    assert "Next" in message
    assert "the user likes cats" in message, "the session's memory never reached the prompt"


# --- keeping GPU jobs short ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_long_prompt_reaches_the_gpu_as_short_jobs(page):
    # WebLLM's pipeline, reduced to what gpu-jobs.js touches. The real one records every
    # dispatch until something syncs, so a prompt that never syncs is one job -- long
    # enough on a mid-range card for the driver to reset the GPU.
    seen = await page.evaluate("""async () => {
        const { keepGpuJobsShort } = await import('./gpu-jobs.js');
        const log = [];
        const pipeline = {
          prefillChunkSize: 1024,
          device: { sync: async () => log.push('sync') },
          async embedAndForward(inputs, length) { log.push(`forward ${length}`); return 'logits'; },
        };
        const engine = { loadedModelIdToPipeline: new Map([['m', pipeline]]) };
        keepGpuJobsShort(engine, 128);
        keepGpuJobsShort(engine, 128);   // loading again must not wrap twice
        const result = await pipeline.embedAndForward([], 128);
        await pipeline.embedAndForward([], 1);
        return { chunk: pipeline.prefillChunkSize, log, result };
    }""")
    assert seen["chunk"] == 128, "the compiled chunk size was left in place"
    assert seen["log"] == ["forward 128", "sync", "forward 1"], (
        "each prompt chunk has to finish before the next is queued; a decoded token needs no wait"
    )
    assert seen["result"] == "logits"

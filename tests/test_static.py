"""The app served as plain static files, with no server of its own.

This is the whole point of moving the turn protocol into the browser: a copy of
frontend/ on a CDN, a file:// path or someone's GitHub Pages is a working app. There is
nothing for Ollama to talk to there, so the only thing that can work is a model running
in the tab -- and the app has to notice that by itself rather than offering a choice
that cannot work.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
# Headless Chrome reports navigator.gpu but hands back no adapter unless asked nicely.
GPU_ARGS = ["--no-sandbox", "--enable-unsafe-webgpu", "--enable-features=Vulkan"]


@pytest.fixture(scope="module")
def static_site():
    """frontend/ over plain HTTP. No /chat, no /models, no /fetch."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port)],
        cwd=FRONTEND, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    yield f"http://localhost:{port}/"
    proc.terminate()
    proc.wait(timeout=10)


@pytest_asyncio.fixture
async def browser_args(request):
    return request.param


async def open_site(url, args):
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True, args=args)
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(e.message))
        await page.goto(url, wait_until="load")
        await page.wait_for_function(
            "document.getElementById('engine').options.length > 0", timeout=20000,
        )
        await page.wait_for_timeout(1200)
        state = {
            "engines": await page.evaluate(
                "[...document.getElementById('engine').options].map(o => o.value)"),
            "selected": await page.evaluate("document.getElementById('engine').value"),
            "model": await page.evaluate("document.getElementById('model').value"),
            "errors": errors,
        }
        await browser.close()
        return state


@pytest.mark.asyncio
async def test_with_no_server_the_app_runs_the_model_in_the_tab(static_site):
    state = await open_site(static_site, GPU_ARGS)
    assert state["engines"] == ["webllm"], (
        "a choice that cannot work was still offered: there is no server to reach Ollama"
    )
    assert state["selected"] == "webllm"
    assert state["model"] == "auto", "no model was chosen, so nothing could be started"
    assert not state["errors"], state["errors"]


@pytest.mark.asyncio
async def test_the_static_app_loads_without_the_backend_it_no_longer_needs(static_site):
    # Every module has to resolve over plain static hosting -- no build step, no bundler,
    # no endpoint that only exists when the Python server is running.
    state = await open_site(static_site, GPU_ARGS)
    assert not state["errors"], state["errors"]

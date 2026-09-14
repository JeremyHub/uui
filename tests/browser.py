"""Watching the app the way a person does: what appears, and what it cost.

Deliberately free of app internals. Earlier versions of these tests read `busy`,
`predictions.size` and `log[]` straight out of the page, which meant renaming a variable
broke the suite and a passing suite proved only that those variables still existed.

Everything here is observed from outside instead:

  - whether the model was consulted, by counting POSTs to /turn
  - whether the screen changed, by watching the iframe's HTML
  - when it settled, by waiting for both to go quiet

Those three hold no matter how the app is built, so they keep working across a rewrite
and they measure what a user would actually notice.
"""

import asyncio
import time

IFRAME_HTML = "document.getElementById('app').contentDocument?.body?.innerHTML ?? ''"
IFRAME_TEXT = "document.getElementById('app').contentDocument?.body?.innerText ?? ''"


class Watcher:
    """Counts model calls and tracks when the screen last changed."""

    def __init__(self, page):
        self.page = page
        self.started = 0
        self.finished = 0
        page.on("request", self._request)
        page.on("requestfinished", self._done)
        page.on("requestfailed", self._done)

    def _request(self, request):
        if request.url.endswith("/turn") and request.method == "POST":
            self.started += 1

    def _done(self, request):
        if request.url.endswith("/turn") and request.method == "POST":
            self.finished += 1

    @property
    def in_flight(self):
        return self.started - self.finished

    async def html(self):
        return await self.page.evaluate(IFRAME_HTML)

    async def text(self):
        return await self.page.evaluate(IFRAME_TEXT)

    async def settle(self, quiet=0.5, limit=30):
        """Wait until no model call is in flight and the screen has stopped changing.

        Polling the rendered HTML rather than asking the app whether it is busy: the
        app's own idea of busy is an internal, and a screen that has stopped changing
        with nothing in flight is the thing a user actually perceives as "done".
        """
        deadline = time.monotonic() + limit
        last, last_change = await self.html(), time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            now = await self.html()
            if now != last:
                last, last_change = now, time.monotonic()
                continue
            if self.in_flight == 0 and time.monotonic() - last_change > quiet:
                return
        raise AssertionError("screen never settled")

    async def time_to_change(self, action, limit=30):
        """How long after `action` the screen first changes, and whether the app had to
        ask the model to find out.

        Compare the timing against a cold run of the same click to tell a served-from-
        cache update from one the app had to fetch. `in_flight_at_change` is reported for
        diagnosis only -- a click that aborts an in-flight guess still shows a request
        outstanding for a moment, so it is not safe to assert on.
        """
        before = await self.html()
        t0 = time.monotonic()
        await action()
        deadline = t0 + limit
        while time.monotonic() < deadline:
            if await self.html() != before:
                return {"seconds": time.monotonic() - t0, "in_flight_at_change": self.in_flight}
            await asyncio.sleep(0.02)
        raise AssertionError("the screen never changed")

    async def turns_taken(self, action, quiet=0.5):
        """Run `action`, then report how many model calls it cost and what changed."""
        before_calls, before_html = self.started, await self.html()
        t0 = time.monotonic()
        await action()
        await self.settle(quiet=quiet)
        return {
            "calls": self.started - before_calls,
            "changed": (await self.html()) != before_html,
            "seconds": time.monotonic() - t0,
        }


async def start_app(page, watcher, concept="a test app", predict=False,
                    url="http://localhost:8765/"):
    await page.goto(url, wait_until="load")
    await set_prediction(page, predict)
    await page.fill("#concept", concept)
    await page.click("#start-btn")
    await watcher.settle()


async def set_prediction(page, enabled):
    """Toggle the app's own "predict next click" checkbox.

    A visible control, so using it keeps these tests outside the app's internals. Tests
    that measure what one click costs turn it off, because otherwise the guessing that
    starts the moment a turn ends gets counted against the click that preceded it.
    """
    await page.set_checked("#speculate", enabled)


async def click_text(page, text):
    """Click by what it says, the way a person picks a thing to click."""
    frame = page.frames[1]
    element = await frame.query_selector(f'text="{text}"')
    assert element, f"nothing on screen says {text!r}"
    await element.click()

"""Measure the real app, with a real model, the way a person would experience it.

tests/ proves the app behaves correctly against a stubbed model in seconds. This is the
other question: with an actual model on an actual GPU, how long does a turn take, and how
often does the app avoid needing one at all.

Like the tests, it observes from outside -- counting POSTs to /turn and watching the
iframe's HTML -- so it keeps working across rewrites of the app's internals. The one thing
it owns is its own idea of what a person would click, which is a model of the user, not of
the app.

    uv run uvicorn backend.main:app --port 8765 &
    uv run python3 scripts/e2e.py [--concept "..."] [--turns 4]
"""

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.browser import Watcher, set_prediction  # noqa: E402

# What a person would try to click, decided here rather than asked of the app. Leaf-most
# matches only: a tab strip and each tab inside it both look clickable to a selector, but
# only the tabs are what anyone aims at.
CLICKABLE_JS = """() => {
  const d = document.getElementById('app').contentDocument;
  const looksClickable = (el) =>
    el.matches('button, a, [role="button"], [onclick], input[type="submit"]') ||
    d.defaultView.getComputedStyle(el).cursor === 'pointer';
  const all = [...d.querySelectorAll('*')]
    .filter((el) => el.textContent.trim() && el.getClientRects().length && looksClickable(el));
  return all
    .filter((el) => !all.some((other) => other !== el && el.contains(other)))
    .map((el) => el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40));
}"""

CLICK_JS = """(label) => {
  const d = document.getElementById('app').contentDocument;
  const hit = [...d.querySelectorAll('*')].find(
    (el) => el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40) === label
            && el.getClientRects().length
            && !el.querySelector('*')?.textContent?.trim()
  ) || [...d.querySelectorAll('*')].find(
    (el) => el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 40) === label
  );
  if (hit) hit.click();
  return !!hit;
}"""


async def run(url, concept, turns, predict):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(e.message))
        watcher = Watcher(page)

        await page.goto(url, wait_until="load")
        await set_prediction(page, predict)
        await page.fill("#concept", concept)

        first_paint = await watcher.time_to_change(lambda: page.click("#start-btn"), limit=180)
        await watcher.settle(quiet=1.0, limit=300)
        regions = await page.evaluate(
            "[...document.getElementById('app').contentDocument"
            ".querySelectorAll('[data-region]')].map(e => e.getAttribute('data-region'))"
        )
        print(f"first screen   settled, first paint {first_paint['seconds']:5.1f}s  "
              f"regions={regions}")
        if len(regions) < 2:
            print("  WARNING: one region means every update rewrites the whole page")

        free, paid = [], []
        for i in range(turns):
            labels = await page.evaluate(CLICKABLE_JS)
            if not labels:
                print("  nothing on screen looks clickable")
                break
            label = labels[i % len(labels)]
            before_html = await watcher.html()
            # Time to the screen finishing, not to everything going quiet: guessing
            # starts the moment a turn ends, and waiting for it would report a click as
            # having taken as long as the work done after it.
            result = await watcher.time_to_stable(
                lambda: page.evaluate(CLICK_JS, label), quiet=1.0, limit=300
            )
            seconds, calls = result["seconds"], result["calls"]
            changed = (await watcher.html()) != before_html
            await watcher.settle(quiet=1.0, limit=300)
            (paid if calls else free).append(seconds)
            cost = (f"{calls} model call" + ("s" if calls != 1 else "")) if calls else "instant"
            note = "" if changed else "  NOTHING CHANGED"
            print(f"  click {label[:30]!r:32} {seconds:5.1f}s  {cost}{note}")

        if paid:
            print(f"\nturns that asked the model: {len(paid)}, mean {sum(paid)/len(paid):.1f}s")
        if free:
            print(f"turns handled without the model: {len(free)}, mean {sum(free)/len(free):.1f}s")
        print("page errors:", errors[:3] or "none")
        await browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8765/")
    ap.add_argument("--concept", default="a recipe browser with category tabs and a search box")
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--predict", action="store_true", help="leave next-click guessing on")
    asyncio.run(run(**vars(ap.parse_args())))


if __name__ == "__main__":
    main()

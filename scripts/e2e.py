"""Drive the real app in a real browser and report what a user would actually feel.

scripts/bench.py measures the server; this measures the product. Everything that makes
UUI fast lives in the frontend -- streaming the first document into the iframe's parser,
swapping single regions, letting data-local controls run without a model call, spending
idle time predicting the next click -- and none of it is exercised by hitting /turn.
Three real bugs (a missing function, a frozen status bar, permanently dead links) were
invisible to the server benchmark and obvious here.

    uv run uvicorn backend.main:app --port 8000 &
    uv run python3 scripts/e2e.py [--concept "..."]
"""

import argparse
import asyncio
import json
import time

from playwright.async_api import async_playwright

# Ask the app itself what is clickable rather than guessing with a selector. Generated
# pages build tabs out of <div> and navs out of hrefless <a>; a narrower selector here
# reports "no controls on screen" for pages the app handles perfectly well.
CLICKABLE_JS = """(() => {
  const d = document.getElementById('app').contentDocument;
  return allControls(d).map(e => e.textContent.trim().slice(0, 40));
})()"""
CLICK_BY_TEXT_JS = """(text) => {
  const d = document.getElementById('app').contentDocument;
  const el = allControls(d).find(e => e.textContent.trim().slice(0, 40) === text);
  if (el) el.click();
  return !!el;
}"""
# Compare region contents, not lengths: a local toggle usually flips a class without
# changing a single character count, and calling that "nothing happened" hides the
# zero-latency path that is the whole point of data-local.
REGION_JS = (
    "[...document.getElementById('app').contentDocument.querySelectorAll('[data-region]')]"
    ".map(e => [e.getAttribute('data-region'), e.innerHTML])"
)


async def wait_idle(pg, limit=240):
    """Settle means the app is idle -- including any data-local fallback turn, which
    starts a beat after the click rather than on it."""
    t0 = time.monotonic()
    await asyncio.sleep(0.8)
    while time.monotonic() - t0 < limit:
        if not await pg.evaluate("busy"):
            return time.monotonic() - t0
        await asyncio.sleep(0.1)
    return None


async def run(concept, turns):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(e.message))

        await page.goto("http://localhost:8000/", wait_until="load")
        await page.fill("#concept", concept)
        await page.click("#start-btn")
        secs = await wait_idle(page)
        regions = dict(await page.evaluate(REGION_JS))
        print(f"shell            {secs:6.1f}s   regions={list(regions)}")
        if len(regions) < 2:
            print("  WARNING: fewer than two regions -- nothing to patch partially")

        times, hits = [], 0
        turns_logged = await page.evaluate("log.length")
        for i in range(turns):
            controls = await page.evaluate(CLICKABLE_JS)
            if not controls:
                print("  no controls on screen -- nothing to click")
                break
            label = controls[i % len(controls)]
            predicted_before = await page.evaluate("predictions.size")
            t0 = time.monotonic()
            if not await page.evaluate(CLICK_BY_TEXT_JS, label):
                print(f"  {label!r} vanished before it could be clicked")
                continue
            await wait_idle(page)
            elapsed = time.monotonic() - t0
            after = dict(await page.evaluate(REGION_JS))
            changed = [k for k in after if regions.get(k) != after[k]]
            entry = await page.evaluate("log[log.length-1]") or {}
            took_turn = await page.evaluate("log.length") > turns_logged
            turns_logged = await page.evaluate("log.length")
            if entry.get("predicted"):
                hits += 1
            regions = after
            if took_turn:
                times.append(elapsed)
            how = ("PREDICTED" if entry.get("predicted") else
                   "model turn" if took_turn else
                   "handled in-page, no model call" if changed else
                   "DEAD CLICK -- nothing happened")
            print(f"  click {label[:26]!r:28} {elapsed:6.1f}s  changed={changed or ['nothing']}"
                  f"  [{how}]  (cache had {predicted_before})")
            if entry.get("plan"):
                print(f"{'':36} plan: {entry['plan'][:78]}")

        # Idle time is when predictions get made, so give it some.
        await wait_idle(page)
        for _ in range(90):
            if await page.evaluate("predictions.size"):
                break
            await asyncio.sleep(1)
        cached = await page.evaluate("predictions.size")
        print(f"\npredictions cached while idle: {cached}")
        if cached:
            target = json.loads((await page.evaluate("[...predictions.keys()]"))[0])[2]
            if await page.evaluate(CLICK_BY_TEXT_JS, target[:40]):
                t0 = time.monotonic()
                await wait_idle(page)
                entry = await page.evaluate("log[log.length-1]") or {}
                print(f"predicted click {target[:26]!r}: {time.monotonic() - t0:.2f}s "
                      f"predicted={bool(entry.get('predicted'))}")

        if times:
            print(f"\nmodel turns: mean {sum(times)/len(times):.1f}s over {len(times)}, "
                  f"{hits} served from prediction")
        print("page errors:", errors[:5] or "none")
        await browser.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concept", default="a todo list with add, complete and delete buttons")
    ap.add_argument("--turns", type=int, default=4)
    args = ap.parse_args()
    asyncio.run(run(args.concept, args.turns))


if __name__ == "__main__":
    main()

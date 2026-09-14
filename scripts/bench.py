"""Drive the running server through a realistic session and report timings.

Runs the real /turn endpoint, so what it measures is what the browser would feel:
time to first visible change, time to settle, and how much HTML the model actually had
to write. Region extraction mirrors what the frontend reads off the live DOM.

Usage:
    uv run uvicorn backend.main:app --port 8000 &
    uv run python3 scripts/bench.py [--concept "..."] [--out DIR]
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.screen import find_regions  # noqa: E402

SERVER = "http://localhost:8000"
STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL)


def stream(payload):
    req = urllib.request.Request(
        f"{SERVER}/turn",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    events, first = [], None
    with urllib.request.urlopen(req, timeout=900) as resp:
        for line in resp:
            if not line.strip():
                continue
            event = json.loads(line)
            if first is None and event["type"] in ("screen_delta", "region", "screen"):
                first = time.monotonic() - t0
            events.append(event)
    return events, time.monotonic() - t0, first


def split(html):
    return find_regions(html), "\n".join(STYLE_RE.findall(html))


def apply_patch(regions, events):
    """Mirror the frontend's region swap so the next turn sees an evolving page."""
    by_id = {r["id"]: r for r in regions}
    for event in events:
        if event["type"] == "region":
            if event["id"] in by_id:
                by_id[event["id"]]["html"] = event["html"]
            else:
                regions.append({"id": event["id"], "html": event["html"]})
                by_id[event["id"]] = regions[-1]
        elif event["type"] == "screen":
            new, _ = split(event["html"])
            regions[:] = new or regions
            by_id = {r["id"]: r for r in regions}
    return regions


SCENARIO = [
    ("submit the search/filter form", {
        "event": "submit",
        "elementData": {"tag": "button", "text": "Filter", "type": "submit"},
        "formValues": {"q": "siamese"},
    }),
    ("click the first result", {
        "event": "click",
        "elementData": {"tag": "h2", "text": "Siamese"},
        "formValues": {},
    }),
    ("click a nav link", {
        "event": "click",
        "elementData": {"tag": "a", "text": "About", "href": "#"},
        "formValues": {},
    }),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concept", default="a cat photo gallery with breeds")
    ap.add_argument("--out", default="/tmp/uui-bench")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    events, total, first = stream(
        {"concept": args.concept, "mode": "shell", "action": {"event": "start", "concept": args.concept}}
    )
    doc = next((e["html"] for e in events if e["type"] == "screen_end"), "")
    (out / "0_shell.html").write_text(doc)
    regions, styles = split(doc)
    print(f"shell                    {total:6.1f}s  first paint {first or 0:5.1f}s  "
          f"{len(doc):5d} chars  regions={[r['id'] for r in regions]}")

    rows = []
    for i, (label, action) in enumerate(SCENARIO, 1):
        events, total, first = stream({
            "concept": args.concept, "regions": regions, "styles": styles,
            "action": action, "recent": [], "mode": "patch",
        })
        written = sum(len(e.get("html", "")) for e in events if e["type"] in ("region", "screen"))
        touched = [e["id"] for e in events if e["type"] == "region"]
        if any(e["type"] == "screen" for e in events):
            touched.append("(whole screen)")
        plan = next((e["text"] for e in events if e["type"] == "plan"), "")
        print(f"{label:24s} {total:6.1f}s  first change {first or 0:5.1f}s  "
              f"{written:5d} chars  changed={touched or ['nothing']}")
        if plan:
            print(f"{'':24s} plan: {plan[:100]}")
        apply_patch(regions, events)
        (out / f"{i}_{label.split()[1]}.html").write_text(
            f"<style>{styles}</style>" + "".join(
                f'<section data-region="{r["id"]}">{r["html"]}</section>' for r in regions
            )
        )
        rows.append(total)

    if rows:
        print(f"\npatch turns: mean {sum(rows)/len(rows):.1f}s, "
              f"min {min(rows):.1f}s, max {max(rows):.1f}s")
    print(f"wrote screens to {out}/")


if __name__ == "__main__":
    main()

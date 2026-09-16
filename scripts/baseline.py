"""Replay the pre-region pipeline so the speedup is measured, not remembered.

Before regions, every turn ran four sequential model calls -- intent, plan, generate,
summary -- and the generate call rewrote the entire HTML document from scratch each time.
The prompts below are copied verbatim from commit 08b67ad so the comparison is honest.

    uv run python3 scripts/baseline.py [--model qwen2.5-coder:3b] [--turns 2]

Compare against scripts/bench.py, which measures the same scenario on the current design.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\n?|\n?```\s*$")
DOCUMENT_RE = re.compile(r"<(?:!doctype|html)\b.*</html>", re.IGNORECASE | re.DOTALL)

INTENT_SYSTEM_PROMPT = """
Given:
- SUMMARY OF CURRENT SCREEN: a plain-text description of what's shown right now.
- CURRENT HTML: the actual markup on screen right now -- use it to
  resolve details the summary leaves out (exact wording, structure).
- ACTION: the single UI event the user just triggered, as JSON.
- elementData

Write a short plain-text statement of the user's intent: what they were trying to accomplish with
this action, grounded in the actual data they entered (formValues) and the actual thing they interacted with (elementData).

Output ONLY the intent statement, a sentence or two.
"""

PLAN_SYSTEM_PROMPT = """Given:
- SUMMARY OF CURRENT SCREEN: a plain-text description of what's shown right now.
- USER INTENT: a plain-text statement of what the user was trying to do with their last action.

Decide what screen should result from the user's intent actually being satisfied.
NO LOADING SCREENS OR PLACEHOLDERS, REAL CONTENT ONLY.

Write a plain-text plan covering:
- What this screen/state is.
- The actual content to show.

Output ONLY the plan. No HTML, no commentary about this task itself.
"""

GENERATE_SYSTEM_PROMPT = """You render a single-page app live, as ONE COMPLETE HTML DOCUMENT.

Reply with ONLY the raw HTML document:
- Full document -- start with <!DOCTYPE html> and include <html>, <head> (with <title> and any
  <meta>/<style> you need), and <body>. No code fences, no commentary.
- Design full-height/full-width -- the document fills the whole viewport (e.g. html, body { height:
  100%; margin: 0; }).
- Make it look genuinely good: real CSS -- typography, color, spacing, flexbox/grid, transitions.
- Use real <img> tags: link real URLs you believe exist for logos/photos.
- <script> runs normally in the document -- DOMContentLoaded/window.onload fire for real, so it's
  fine to use them.
- DO NOT include ANY PLACEHOLDER TEXT. Be creative with text. No loading screens or placeholders.
"""

SUMMARY_SYSTEM_PROMPT = """Write a concise plain-text summary (no HTML, no code fences) covering:
- What screen/state this is and its purpose.
- The concrete content actually shown.
- Layout/visual identity worth remembering (theme, colors, whether it replicates a real product's
  look).

Output ONLY the summary. No HTML, no commentary about this task itself.
"""


def call(model, system, user):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "keep_alive": "30m",
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=900) as resp:
        data = json.loads(resp.read())
    return FENCE_RE.sub("", data["message"]["content"]).strip(), time.monotonic() - t0


def turn(model, html, summary, action, is_start):
    """One turn of the old pipeline: intent, plan, generate, summary."""
    phases = {}
    if is_start:
        intent = f"The user wants an app based on the following concept: {action.get('concept', '')}"
        phases["intent"] = 0.0
    else:
        intent, phases["intent"] = call(
            model, INTENT_SYSTEM_PROMPT,
            f"SUMMARY OF CURRENT SCREEN:\n{summary}\n\nCURRENT HTML:\n{html}\n\n"
            f"ACTION:\n{json.dumps(action, indent=2)}",
        )
    plan, phases["plan"] = call(
        model, PLAN_SYSTEM_PROMPT,
        f"SUMMARY OF CURRENT SCREEN:\n{summary}\n\nUSER INTENT:\n{intent}",
    )
    raw, phases["html"] = call(model, GENERATE_SYSTEM_PROMPT, f"PLAN:\n{plan}")
    match = DOCUMENT_RE.search(raw)
    new_html = match.group(0) if match else raw
    new_summary, phases["summary"] = call(model, SUMMARY_SYSTEM_PROMPT, f"HTML:\n{new_html}")
    return new_html, new_summary, phases


SCENARIO = [
    {"event": "submit", "elementData": {"tag": "button", "text": "Filter", "type": "submit"},
     "formValues": {"q": "siamese"}},
    {"event": "click", "elementData": {"tag": "h2", "text": "Siamese"}, "formValues": {}},
    {"event": "click", "elementData": {"tag": "a", "text": "About", "href": "#"}, "formValues": {}},
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen2.5-coder:3b")
    ap.add_argument("--concept", default="a cat photo gallery with breeds")
    ap.add_argument("--turns", type=int, default=3)
    args = ap.parse_args()

    html, summary = "", ""
    html, summary, phases = turn(
        args.model, html, summary, {"event": "start", "concept": args.concept}, True
    )
    total = sum(phases.values())
    print(f"shell (old)              {total:6.1f}s  {len(html):5d} chars  "
          + "  ".join(f"{k}={v:.1f}s" for k, v in phases.items()))

    times = []
    for i, action in enumerate(SCENARIO[: args.turns]):
        html, summary, phases = turn(args.model, html, summary, action, False)
        total = sum(phases.values())
        times.append(total)
        label = f"{action['event']} {action['elementData']['text']!r}"
        print(f"{label:24s} {total:6.1f}s  {len(html):5d} chars  "
              + "  ".join(f"{k}={v:.1f}s" for k, v in phases.items()))

    if times:
        print(f"\nold pipeline turns: mean {sum(times)/len(times):.1f}s over {len(times)}")


if __name__ == "__main__":
    main()

"""Replay the app's real prompt contract against a given Ollama model, outside the server.

Runs a standardized two-turn scenario (bootstrap a concept, then submit a form) directly
against Ollama's /api/chat using the exact SYSTEM_PROMPT and CURRENT_HTML/ACTION TIMELINE
format backend/main.py builds, so model output is directly comparable to what the app would
actually generate -- without needing the FastAPI server running.

Usage:
    uv run python3 scripts/test_model.py <model> [outdir] [--concept "..."] [--query "..."]

Writes 1_bootstrap.html and 2_action.html into outdir (default: cwd), and prints timing/size
to stdout. Compare candidate models with `ollama ps` alongside this to see GPU/CPU split.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.main import SYSTEM_PROMPT, FENCE_RE  # noqa: E402

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def call(model: str, current_html: str, actions: list[dict]) -> str:
    user_content = (
        f"CURRENT HTML:\n{current_html or '(empty -- nothing rendered yet)'}\n\n"
        f"ACTION TIMELINE (oldest first):\n{json.dumps(actions, indent=2)}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    elapsed = time.monotonic() - start
    content = FENCE_RE.sub("", data["message"]["content"])
    print(f"  {elapsed:5.1f}s, {len(content):5d} chars", file=sys.stderr)
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="Ollama model tag, e.g. gemma3:4b")
    parser.add_argument("outdir", nargs="?", default=".", help="Directory to write HTML files into")
    parser.add_argument("--concept", default="google", help="Bootstrap concept for the start event")
    parser.add_argument("--action", default="search", help="data-action value for the follow-up event")
    parser.add_argument("--query", default="blogs about cats", help="Value to put in the form field")
    parser.add_argument("--field", default="q", help="Form field name the value goes under")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[1/2] bootstrap concept={args.concept!r}", file=sys.stderr)
    html1 = call(args.model, "", [{"event": "start", "concept": args.concept}])
    (outdir / "1_bootstrap.html").write_text(html1)

    actions2 = [
        {"event": "start", "concept": args.concept},
        {
            "event": "submit",
            "action": args.action,
            "elementData": {},
            "formValues": {args.field: args.query},
        },
    ]
    print(f"[2/2] action={args.action!r} {args.field}={args.query!r}", file=sys.stderr)
    html2 = call(args.model, html1, actions2)
    (outdir / "2_action.html").write_text(html2)

    print(f"wrote {outdir}/1_bootstrap.html and {outdir}/2_action.html", file=sys.stderr)


if __name__ == "__main__":
    main()

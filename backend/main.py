"""UUI backend: streams live-generated UI from a local Ollama model.

Two kinds of turn, both served by POST /turn as an NDJSON event stream:

  shell  -- the first turn. One call produces a whole HTML document, streamed straight
            through so the frontend can parse it into the iframe as it arrives.
  patch  -- every turn after that. One call produces only the regions that change,
            in a line-marker format the parser below splits so each region can be
            applied the instant it finishes, not when the response does.

Everything the old pipeline spent extra model calls on (intent, plan, summary) is either
folded into the single call's first line or computed deterministically by the frontend
from the live DOM. Output tokens are ~7x more expensive than input tokens on a local GPU,
so writing 300 characters of one region instead of 4000 characters of a whole document is
where nearly all of the speedup comes from.
"""

import json
import os
import re
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from backend.prompts import PATCH_SYSTEM_PROMPT, SHELL_SYSTEM_PROMPT
from backend.screen import ELISION_RE, render_screen

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# One model for both phases on purpose: on a 4GB card a second model evicts the first,
# and paying a 4-7s reload every turn costs more than any per-phase model choice saves.
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b")
SHELL_MODEL = os.environ.get("UUI_SHELL_MODEL", MODEL)
PATCH_MODEL = os.environ.get("UUI_PATCH_MODEL", MODEL)
# Long enough that the model never unloads between turns during a session.
KEEP_ALIVE = os.environ.get("UUI_KEEP_ALIVE", "30m")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

FENCE_LINE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*$")
BODY_WRAP_RE = re.compile(r"^<body\b[^>]*>|</body>$", re.IGNORECASE)

app = FastAPI()


class Turn(BaseModel):
    concept: str = ""
    # What's on screen, read straight off the live iframe DOM by the frontend: one
    # entry per addressable region, plus the document's CSS. Replaces the old
    # LLM-generated prose summary -- it costs no model call, it cannot drift from what
    # is actually rendered, and unlike prose it carries the markup the patch has to
    # match. Compacted server-side (backend/screen.py) before it reaches the prompt.
    regions: list[dict] = []
    styles: str = ""
    action: dict = {}
    recent: list[str] = []
    mode: str = "patch"
    model: str | None = None
    speculative: bool = False


class OllamaError(Exception):
    pass


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/base.css")
async def base_css():
    # The design system generated pages are written against. Serving it rather than
    # having the model write one saves ~350 output tokens on every first screen, and
    # keeps it out of every patch prompt thereafter.
    return FileResponse(FRONTEND_DIR / "base.css", media_type="text/css")


async def ollama_stream(client, model, system, user, num_predict, temperature):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "options": {"num_predict": num_predict, "temperature": temperature},
    }
    async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
        if resp.status_code >= 400:
            body = (await resp.aread()).decode(errors="replace")
            raise OllamaError(f"Ollama error ({resp.status_code}): {body}")
        async for line in resp.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise OllamaError(f"Ollama error: {chunk['error']}")
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece


def build_patch_user_message(req: Turn) -> str:
    parts = [f"APP CONCEPT:\n{req.concept or '(unspecified)'}"]
    if req.recent:
        parts.append("RECENTLY:\n" + "\n".join(f"- {r}" for r in req.recent[-3:]))
    parts.append("SCREEN:\n" + (render_screen(req.regions, req.styles) or "(nothing yet)"))
    parts.append(f"USER ACTION:\n{json.dumps(req.action, indent=2)}")
    return "\n\n".join(parts)


def clean_output(html: str) -> str:
    """Scrub compaction artifacts the model copied out of its own prompt.

    Small models treat the elided screen as a template and will happily echo
    "<!-- and 5 more <li> ... -->" straight into the page. The marker exists only to
    save prompt tokens, so it is stripped unconditionally on the way back out.
    """
    return ELISION_RE.sub("", html).strip()


class PatchParser:
    """Incremental line parser for the #plan / #region / #screen / #end format.

    Yields events as soon as a marker line proves the previous block is finished, so a
    region reaches the screen while the model is still writing the next one.
    """

    def __init__(self):
        self.buf = ""
        self.kind = None       # "region" | "screen" | None
        self.region_id = None
        self.body = []

    def _flush(self):
        if self.kind is None:
            return None
        html = clean_output("".join(self.body))
        event = None
        if html:
            if self.kind == "screen":
                # Models often wrap a #screen block in <body> despite being asked for
                # its contents; that tag would land inside the real body if kept.
                html = BODY_WRAP_RE.sub("", html).strip()
                event = {"type": "screen", "html": html}
            else:
                event = {"type": "region", "id": self.region_id, "html": html}
        self.kind, self.region_id, self.body = None, None, []
        return event

    def feed(self, text: str):
        self.buf += text
        while (nl := self.buf.find("\n")) != -1:
            line, self.buf = self.buf[:nl], self.buf[nl + 1:]
            yield from self._line(line)

    def finish(self):
        if self.buf:
            yield from self._line(self.buf)
            self.buf = ""
        if (event := self._flush()):
            yield event

    def _line(self, line: str):
        stripped = line.strip()
        if FENCE_LINE_RE.match(line):
            return
        if stripped.startswith("#plan"):
            if (event := self._flush()):
                yield event
            yield {"type": "plan", "text": stripped[5:].strip()}
        elif stripped.startswith("#region"):
            if (event := self._flush()):
                yield event
            self.kind = "region"
            self.region_id = stripped[7:].strip().strip('"\'') or "main"
            self.body = []
        elif stripped.startswith("#screen"):
            if (event := self._flush()):
                yield event
            self.kind = "screen"
            self.body = []
        elif stripped.startswith("#end"):
            if (event := self._flush()):
                yield event
        elif self.kind is not None:
            self.body.append(line + "\n")


@app.post("/turn")
async def turn(req: Turn):
    started = time.monotonic()

    async def stream():
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                if req.mode == "shell":
                    async for event in run_shell(client, req):
                        yield json.dumps(event) + "\n"
                else:
                    async for event in run_patch(client, req):
                        yield json.dumps(event) + "\n"
            except OllamaError as e:
                yield json.dumps({"type": "error", "message": str(e)}) + "\n"
                return
            except httpx.HTTPError as e:
                # Ollama not running is the single most likely failure here, and letting
                # the connection error escape gives the browser a truncated stream with
                # nothing to show for it. Say what happened instead.
                yield json.dumps({
                    "type": "error",
                    "message": f"Could not reach Ollama at {OLLAMA_URL} ({e.__class__.__name__}). Is it running?",
                }) + "\n"
                return
        yield json.dumps({"type": "done", "elapsed": round(time.monotonic() - started, 2)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


TRAILING_JUNK_RE = re.compile(r"(?:\s|`{3,}[a-zA-Z0-9]*|</body>|</html>)+$", re.IGNORECASE)
# Enough to hold back a closing fence, a </body></html>, and their whitespace.
TAIL_HOLDBACK = 24
BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


def body_content_start(text: str) -> int:
    """Where the model's actual body content begins, or -1 if it is not clear yet.

    The model is asked for body content and nothing else, and mostly complies -- but it
    also opens with "Sure, here it is:" or wraps the lot in a full document. Since the
    reply is streamed into the iframe's parser, junk has to be identified before it is
    written rather than cleaned up afterwards.
    """
    lower = text.lower()
    if (i := lower.find("<section")) != -1:
        return i
    if (m := BODY_OPEN_RE.search(text)):
        return m.end()
    # No structural landmark yet. Once enough has arrived that none is coming, fall back
    # to the first tag of any kind rather than stalling the stream forever.
    if len(text) > 800:
        return text.find("<") if "<" in text else 0
    return -1


async def run_shell(client, req: Turn):
    """Stream the body as it is generated, so the page fills in rather than appearing.

    The document around it -- doctype, head, stylesheet -- belongs to the app and is
    already on screen before this is called, so the model writes content and nothing else.
    """
    model = req.model or SHELL_MODEL
    user = f"APP CONCEPT:\n{req.concept or 'a simple demo app'}"
    yield {"type": "phase", "name": "building"}

    parts = []
    pending = ""
    started = False
    async for piece in ollama_stream(client, model, SHELL_SYSTEM_PROMPT, user, 2200, 0.7):
        parts.append(piece)
        if not started:
            joined = "".join(parts)
            idx = body_content_start(joined)
            if idx == -1:
                continue
            started, pending = True, joined[idx:]
        else:
            pending += piece
        # Hold back the tail: a model that wraps its answer in ```html would otherwise
        # leave the closing fence rendered as text at the bottom of the finished page.
        if len(pending) > TAIL_HOLDBACK:
            yield {"type": "screen_delta", "text": pending[:-TAIL_HOLDBACK]}
            pending = pending[-TAIL_HOLDBACK:]

    full = "".join(parts)
    if not started:
        # The model ignored the format entirely. Better to show what it said than to
        # leave a blank screen with nothing to explain it.
        pending = f'<section data-region="main">{full}</section>'
    tail = TRAILING_JUNK_RE.sub("", pending)
    yield {"type": "screen_delta", "text": tail}
    yield {"type": "screen_end"}


def guard_screen(event: dict, req: Turn) -> dict:
    """Stop a malformed #screen from wiping the app.

    A screen swap replaces all the body content, so getting it wrong is the one failure
    in this design that destroys work rather than just looking wrong. The format asks for
    <section data-region> blocks; a reply with none of them is a region's worth of content
    that the model mislabelled, so treat it as one.
    """
    if event["type"] != "screen" or "data-region" in event["html"]:
        return event
    if not req.regions:
        return event
    # Never land it on the region holding the control that was just clicked. The biggest
    # region on a page is often the navigation, and dropping an About page into the nav
    # deletes every way of getting anywhere -- worse than the blank screen this guards
    # against. Content the user asked for belongs somewhere other than the menu.
    clicked_in = (req.action or {}).get("elementData", {}).get("inRegion")
    candidates = [r for r in req.regions if r["id"] != clicked_in] or req.regions
    biggest = max(candidates, key=lambda r: len(r.get("html", "")))
    return {"type": "region", "id": biggest["id"], "html": event["html"], "demoted": True}


async def run_patch(client, req: Turn):
    model = req.model or PATCH_MODEL
    user = build_patch_user_message(req)
    yield {"type": "phase", "name": "updating"}

    parser = PatchParser()
    async for piece in ollama_stream(client, model, PATCH_SYSTEM_PROMPT, user, 1600, 0.4):
        for event in parser.feed(piece):
            yield guard_screen(event, req)
    for event in parser.finish():
        yield guard_screen(event, req)

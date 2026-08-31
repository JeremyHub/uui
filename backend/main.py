import json
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9]*\n?|\n?```\s*$")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

PLAN_SYSTEM_PROMPT = """You are the planning phase of a two-phase live UI generator. A separate model
will take your plan and turn it into HTML -- you write NO HTML, CSS, or JS yourself, only the plan.

Each request gives you CURRENT HTML (exactly what's on screen right now, empty if nothing yet)
and an ACTION TIMELINE (every UI event so far, oldest first, as JSON):
  {"event": "start", "concept": "<what the app should be>"}
  {"event": "click", "action": "<data-action value, or \"navigate\" for a plain link>", "elementData": {...}, "formValues": {...}}
  {"event": "submit", "action": "<the form's data-action, or \"submit\">", "elementData": {...}, "formValues": {...}}

elementData: the element's data-* attributes (a link also gets {href, linkText}).
formValues: every input/select/textarea's real current state (the browser's live state) at the
moment of the action, keyed by name (or id) -- checkboxes true/false, a radio group its selected
value or null, multi-select an array, else the string value. Always accurate -- trust it completely.

React to the LAST action in the timeline: decide what screen results from it actually happening.
Be creative and commit to fully realized, specific content -- real-sounding titles, names,
numbers, descriptions -- that reads like an actual website, not a mockup. ABSOLUTELY NO
PLACEHOLDER TEXT, "Result 1"/"Lorem ipsum"-style filler, "searching..."/loading/pending states,
or empty stubs left for a future turn. Decide the final, fully populated result yourself, right now.

Write a plain-text plan (no HTML, no code fences) covering:
- What this screen/state is and why it follows from the last action.
- The actual content to show, spelled out concretely -- real titles, numbers, names, copy, not
  categories or placeholders.
- Every interactive element the next turn will need: what it is, its exact label/text, and what
  should happen when it's triggered (this becomes a data-action name and, for inputs, a name
  attribute).
- Visual/identity notes worth carrying forward from CURRENT HTML, including replicating a real
  product/site's actual look (colors, logo, layout, chrome) if the concept names one.

Output ONLY the plan. No HTML, no commentary about this task itself.
"""

GENERATE_SYSTEM_PROMPT = """You render a single-page app live, as ONE HTML FRAGMENT (never a full document), inside a div with id="app" that fills the viewport.

You are the generation phase of a two-phase pipeline: another model already decided what should
appear next and wrote it up as PLAN below. Implement that plan faithfully -- don't invent
different content or second-guess its decisions, just turn it into good HTML/CSS/JS.

You're also given CURRENT HTML (exactly what's on screen right now, empty if nothing yet) purely
for visual continuity (matching fonts, colors, layout conventions already established).

Reply with ONLY the raw HTML fragment to replace #app's contents:
- Fragment only -- no <!DOCTYPE>, <html>, <head>, <title>, <meta>, <body>. No code fences,
  no commentary.
- data-action="..." on anything (besides plain links) that should trigger the next step, matching
  what the plan calls for.
- name="..." on any input/select/textarea whose value matters later.
- Design full-height/full-width -- #app fills the whole viewport.
- Make it look genuinely good: real inline CSS -- typography, color, spacing, flexbox/grid,
  transitions.
- Use real <img> tags: link real URLs you believe exist for logos/photos, or
  <img src="https://picsum.photos/<w>/<h>?random=<n>"> for generic filler.
- Never fetch()/XHR a real external API -- no backend exists for that. Write all data
  directly into the HTML/JS yourself.
- Inline <script> executes (re-inserted after every update). It runs after the page already
  loaded, so never wrap it in DOMContentLoaded/window.onload -- write top-level code. Don't
  use window.location/window.open.
"""

app = FastAPI()


class GenerateRequest(BaseModel):
    current_html: str = ""
    actions: list[dict]
    model: str | None = None


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


class OllamaError(Exception):
    pass


async def ollama_chat_stream(client: httpx.AsyncClient, model: str, messages: list[dict]):
    async with client.stream(
        "POST",
        f"{OLLAMA_URL}/api/chat",
        json={"model": model, "messages": messages, "stream": True},
    ) as resp:
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


@app.post("/generate")
async def generate(req: GenerateRequest):
    model = req.model or MODEL
    current_html = req.current_html or "(empty -- nothing rendered yet)"
    timeline = json.dumps(req.actions, indent=2)

    async def stream():
        # Response is NDJSON so the frontend can surface both phases (plan, then
        # html) as they complete, rather than only seeing the final fragment.
        async with httpx.AsyncClient(timeout=None) as client:
            plan_messages = [
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"CURRENT HTML:\n{current_html}\n\nACTION TIMELINE (oldest first):\n{timeline}",
                },
            ]
            parts = []
            try:
                async for piece in ollama_chat_stream(client, model, plan_messages):
                    parts.append(piece)
                    yield json.dumps({"phase": "plan", "done": False, "chars": sum(len(p) for p in parts)}) + "\n"
            except OllamaError as e:
                yield json.dumps({"phase": "error", "message": str(e)}) + "\n"
                return
            plan_text = FENCE_RE.sub("", "".join(parts)).strip()
            yield json.dumps({"phase": "plan", "done": True, "text": plan_text}) + "\n"

            generate_messages = [
                {"role": "system", "content": GENERATE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"PLAN:\n{plan_text}\n\nCURRENT HTML:\n{current_html}",
                },
            ]
            parts = []
            try:
                async for piece in ollama_chat_stream(client, model, generate_messages):
                    parts.append(piece)
                    yield json.dumps({"phase": "html", "done": False, "chars": sum(len(p) for p in parts)}) + "\n"
            except OllamaError as e:
                yield json.dumps({"phase": "error", "message": str(e)}) + "\n"
                return
            # Buffered rather than streamed piece-by-piece: models often wrap
            # fragments in ```html ... ``` fences, which can only be stripped
            # once the full response is in hand.
            html = FENCE_RE.sub("", "".join(parts))
            yield json.dumps({"phase": "html", "done": True, "text": html}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")

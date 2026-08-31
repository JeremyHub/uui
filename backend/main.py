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

SYSTEM_PROMPT = """You render a single-page app live, as ONE HTML FRAGMENT (never a full document), inside a div with id="app" that fills the viewport.

Each request gives you CURRENT HTML (exactly what's on screen right now, empty if nothing yet)
and an ACTION TIMELINE (every UI event so far, oldest first, as JSON):
  {"event": "start", "concept": "<what the app should be>"}
  {"event": "click", "action": "<data-action value, or \"navigate\" for a plain link>", "elementData": {...}, "formValues": {...}}
  {"event": "submit", "action": "<the form's data-action, or \"submit\">", "elementData": {...}, "formValues": {...}}

elementData: the element's data-* attributes (a link also gets {href, linkText}).
formValues: every input/select/textarea's real current state, keyed by name (or id) --
checkboxes true/false, a radio group its selected value or null, multi-select an array,
else the string value. Always accurate -- trust it completely.

React to the LAST action in the timeline: render the screen that results from it actually
happening. Be creative and commit to fully realized, specific content -- real-sounding
titles, names, numbers, descriptions -- that reads like an actual website, not a mockup.
ABSOLUTELY NO PLACEHOLDER TEXT, "Result 1"/"Lorem ipsum"-style filler, "searching..."/
loading/pending states, or empty stubs left for a future turn. Every screen you output is
the final, fully populated result, invented by you, right now.

Reply with ONLY the raw HTML fragment to replace #app's contents:
- Fragment only -- no <!DOCTYPE>, <html>, <head>, <title>, <meta>, <body>. No code fences,
  no commentary.
- data-action="..." on anything (besides plain links) that should trigger the next step.
- name="..." on any input/select/textarea whose value matters later.
- Design full-height/full-width -- #app fills the whole viewport.
- Make it look genuinely good: real inline CSS -- typography, color, spacing, flexbox/grid,
  transitions.
- If the concept names a real product/site/app, replicate its actual visual identity as best
  you recall (colors, logo, layout, chrome) around fully fabricated content.
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


@app.post("/generate")
async def generate(req: GenerateRequest):
    user_content = (
        f"CURRENT HTML:\n{req.current_html or '(empty -- nothing rendered yet)'}\n\n"
        f"ACTION TIMELINE (oldest first):\n{json.dumps(req.actions, indent=2)}"
    )
    payload_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    async def stream():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": req.model or MODEL,
                    "messages": payload_messages,
                    "stream": True,
                },
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    yield f'<pre style="color:red; white-space:pre-wrap;">Ollama error ({resp.status_code}): {body}</pre>'
                    return

                full = []
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        yield f'<pre style="color:red; white-space:pre-wrap;">Ollama error: {chunk["error"]}</pre>'
                        return
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        full.append(piece)

                # Buffered rather than streamed piece-by-piece: models often wrap
                # fragments in ```html ... ``` fences, which can only be stripped
                # once the full response is in hand.
                yield FENCE_RE.sub("", "".join(full))

    return StreamingResponse(stream(), media_type="text/plain")

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
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

SYSTEM_PROMPT = """You are the live HTML renderer for a single-page web app running inside a div with id="app".

Every "user" role message describes a UI event as JSON:
  {"event": "start", "concept": "<what the user wants the app to be>"}
  {"event": "click", "action": "<the data-action value>", "elementData": {...}, "formValues": {...}}

elementData holds any data-* attributes (other than data-action) on the clicked element.
formValues holds the current value of every named form field on screen at click time.

Reply with ONLY the raw HTML that should replace the entire contents of #app. Rules:
- No <html>, <head>, or <body> tags -- just the fragment.
- No markdown code fences, no commentary, no explanation. Output must start directly with HTML.
- Any element that should trigger the next step (buttons, links, etc.) must have a
  data-action="..." attribute describing what happens, e.g. data-action="submit-guess".
- Any input/select/textarea whose value matters later must have a name="..." attribute.
- Keep the UI visually coherent with what you generated last turn unless the action implies
  a full transition.
- Inline <style> is fine. Inline <script> will NOT execute (it's injected via innerHTML), so
  express all interactivity through data-action + regeneration, not JavaScript.
"""

app = FastAPI()


class Message(BaseModel):
    role: str
    content: str


class GenerateRequest(BaseModel):
    messages: list[Message]
    model: str | None = None


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/generate")
async def generate(req: GenerateRequest):
    payload_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    payload_messages += [m.model_dump() for m in req.messages]

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

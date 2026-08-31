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
# MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

INTENT_SYSTEM_PROMPT = """
Given:
- SUMMARY OF CURRENT SCREEN: a plain-text description of what's shown right now (empty if nothing yet).
- CURRENT HTML: the actual markup on screen right now (empty if nothing yet) -- use it to
  resolve details the summary leaves out (exact wording, structure).
- ACTION: the single UI event the user just triggered, as JSON.
- elementData

Write a short plain-text statement of the user's intent: what they were trying to accomplish with
this action, grounded in the actual data they entered (formValues) and the actual thing they interacted with (elementData).
For a "start" event, the intent is simply to generate the described concept.

Output ONLY the intent statement, a sentence or two.
"""

PLAN_SYSTEM_PROMPT = """Given:
- SUMMARY OF CURRENT SCREEN: a plain-text description of what's shown right now.
- USER INTENT: a plain-text statement of what the user was trying to do with their last action.

Decide what screen should result from the user's intent actually being satisfied.

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
"""

SUMMARY_SYSTEM_PROMPT = """Write a concise plain-text summary (no HTML, no code fences) covering:
- What screen/state this is and its purpose.
- The concrete content actually shown.
- Layout/visual identity worth remembering (theme, colors, whether it replicates a real product's
  look).

Output ONLY the summary. No HTML, no commentary about this task itself.
"""

app = FastAPI()


class GenerateRequest(BaseModel):
    current_html: str = ""
    summary: str = ""
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
    summary = req.summary or "(nothing yet -- this is the first screen)"
    last_action = req.actions[-1] if req.actions else {}
    action_json = json.dumps(last_action, indent=2)

    async def stream():
        # Response is NDJSON so the frontend can surface all four phases (intent,
        # plan, html, summary) as they complete, rather than only seeing the final
        # fragment. Only the intent phase sees the raw current HTML and action --
        # planning works from the summary and the intent statement alone, so it
        # treats the prior screen as replaceable rather than material to preserve.
        async with httpx.AsyncClient(timeout=None) as client:
            intent_messages = [
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"SUMMARY OF CURRENT SCREEN:\n{summary}\n\nCURRENT HTML:\n{current_html}\n\nACTION:\n{action_json}",
                },
            ]
            parts = []
            try:
                async for piece in ollama_chat_stream(client, model, intent_messages):
                    parts.append(piece)
                    yield json.dumps({"phase": "intent", "done": False, "chars": sum(len(p) for p in parts)}) + "\n"
            except OllamaError as e:
                yield json.dumps({"phase": "error", "message": str(e)}) + "\n"
                return
            intent_text = FENCE_RE.sub("", "".join(parts)).strip()
            yield json.dumps({"phase": "intent", "done": True, "text": intent_text}) + "\n"

            plan_messages = [
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"SUMMARY OF CURRENT SCREEN:\n{summary}\n\nUSER INTENT:\n{intent_text}",
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
                {"role": "user", "content": f"PLAN:\n{plan_text}"},
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

            summary_messages = [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": f"HTML:\n{html}"},
            ]
            parts = []
            try:
                async for piece in ollama_chat_stream(client, model, summary_messages):
                    parts.append(piece)
                    yield json.dumps({"phase": "summary", "done": False, "chars": sum(len(p) for p in parts)}) + "\n"
            except OllamaError as e:
                yield json.dumps({"phase": "error", "message": str(e)}) + "\n"
                return
            summary_text = FENCE_RE.sub("", "".join(parts)).strip()
            yield json.dumps({"phase": "summary", "done": True, "text": summary_text}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")

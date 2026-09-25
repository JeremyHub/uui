"""UUI backend: a way to reach Ollama, and a place to serve the app from.

The turn protocol used to live here -- prompts, screen compaction, the reply parser, the
orchestration. It lives in frontend/ now, because the app has to run with no server at
all: a static copy of frontend/ plus a model running in the browser tab is a working
version of this app. Keeping two implementations of the protocol, one per language, was
the alternative, and it was not one.

What is left is what a browser genuinely cannot do for itself:

  POST /chat   stream from Ollama, which does not allow this origin by default, or from
               Claude through the Claude Code CLI (see claude_code.py). The engine in
               frontend/src/engine.ts is the same one that runs in the tab; this is only
               somewhere else for it to run
  GET  /models what Ollama has pulled, and the Claude models on offer
  GET  /fetch  call an external API on the page's behalf, past CORS

Serve frontend/ as static files from anywhere and the app still works; it just needs a
model in the tab instead.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import claude_code

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

FETCH_TIMEOUT = 8.0
# Enough for a useful API response, small enough that a turn's prompt stays affordable.
FETCH_MAX_BYTES = 64 * 1024

app = FastAPI()


@app.middleware("http")
async def revalidate(request: Request, call_next):
    """Have the browser check for a newer copy of the app on every load.

    With no Cache-Control, a browser decides for itself how long a file stays fresh --
    a tenth of the time since it last changed -- so a refresh after an edit could keep
    running the old modules for many minutes. An unchanged file still costs only a 304.
    """
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/models")
async def models():
    """What Ollama has pulled, and the Claude models, for the model picker."""
    claude = claude_code.models()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            tags = response.json().get("models", [])
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"models": claude,
             "error": f"Could not reach Ollama at {OLLAMA_URL} ({type(exc).__name__})."},
            status_code=200,
        )
    pulled = [{"id": m["name"], "sizeMB": round(m.get("size", 0) / 1e6)} for m in tags]
    return {"models": pulled + claude}


@app.get("/fetch")
async def fetch(url: str):
    """Call an API on the generated page's behalf.

    The page is sandboxed in an iframe and CORS stops it calling most APIs directly, so
    this is the only way a generated app can show real data. Any host the model names is
    called: which API a page needs is the model's decision, not a list's. Refusals come
    back as HTTP 200 with an `error` field, like /models, because the caller is a
    model-driven UI that has to render something sensible rather than crash.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"url": url, "error": "Only http and https URLs can be fetched."}

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            # No client headers are forwarded and no cookies are kept: the request is
            # anonymous, so nothing the browser holds can leak through it.
            async with client.stream("GET", url, headers={"Accept": "application/json, text/*"}) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= FETCH_MAX_BYTES:
                        break
                text = bytes(body[:FETCH_MAX_BYTES]).decode("utf-8", errors="replace")
                return {
                    "url": url,
                    "status": response.status_code,
                    "contentType": response.headers.get("content-type", ""),
                    "body": text,
                    "truncated": len(body) >= FETCH_MAX_BYTES,
                }
    except httpx.HTTPError as exc:
        return {"url": url, "error": f"Could not reach {parts.hostname} ({type(exc).__name__})."}


@app.post("/chat")
async def chat(request: Request):
    """Stream Ollama's chat response through unchanged.

    Deliberately dumb: the body is Ollama's own request format and the response is
    Ollama's own NDJSON. Nothing here knows what a region is, so nothing here has to
    change when the protocol does. A Claude model gets the same format back, translated
    from the Claude Code CLI.
    """
    payload = await request.body()
    try:
        model = json.loads(payload).get("model", "")
    except ValueError:
        model = ""
    if claude_code.is_claude(model):
        return StreamingResponse(
            claude_code.stream_chat(json.loads(payload)), media_type="application/x-ndjson",
        )

    async def stream():
        client = httpx.AsyncClient(timeout=None)
        try:
            async with client.stream(
                "POST", f"{OLLAMA_URL}/api/chat", content=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    yield _error_line(f"Ollama returned {response.status_code}: {body[:400]}")
                    return
                async for chunk in response.aiter_raw():
                    yield chunk
        except httpx.HTTPError as exc:
            # Ollama not running is the most likely failure in this whole app, and an
            # unexplained truncated stream is the worst way to report it.
            yield _error_line(
                f"Could not reach Ollama at {OLLAMA_URL} ({type(exc).__name__}). Is it running?"
            )
        finally:
            await client.aclose()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _error_line(message: str) -> bytes:
    return (json.dumps({"error": message}) + "\n").encode()


# Everything else is the app itself. Mounted last so the routes above win.
app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")

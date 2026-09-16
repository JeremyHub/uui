"""UUI backend: a way to reach Ollama, and a place to serve the app from.

The turn protocol used to live here -- prompts, screen compaction, the reply parser, the
orchestration. It lives in frontend/ now, because the app has to run with no server at
all: a static copy of frontend/ plus a model running in the browser tab is a working
version of this app. Keeping two implementations of the protocol, one per language, was
the alternative, and it was not one.

What is left is what a browser genuinely cannot do for itself:

  POST /chat   stream from Ollama, which does not allow this origin by default
  GET  /models what Ollama has pulled
  GET  /fetch  call an external API on the page's behalf, past CORS and behind an
               allowlist

Serve frontend/ as static files from anywhere and the app still works; it just needs a
model in the tab instead.
"""

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# The allowlist is the security boundary for /fetch. That endpoint takes a URL chosen by
# a language model and makes a request with it, so without this it is an open relay
# sitting inside whatever network the server runs on. Keep it to APIs that are public,
# key-free, and read-only.
DEFAULT_API_HOSTS = (
    "open-meteo.com", "restcountries.com", "hacker-news.firebaseio.com",
    "api.coingecko.com", "wikipedia.org", "pokeapi.co", "api.tvmaze.com",
    "api.github.com", "datausa.io", "api.openbrewerydb.org",
)
API_HOSTS = tuple(
    h.strip().lower() for h in
    os.environ.get("UUI_API_ALLOWLIST", ",".join(DEFAULT_API_HOSTS)).split(",")
    if h.strip()
)
FETCH_TIMEOUT = 8.0
# Enough for a useful API response, small enough that a turn's prompt stays affordable.
FETCH_MAX_BYTES = 64 * 1024

app = FastAPI()


@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/models")
async def models():
    """What Ollama has pulled, for the model picker."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            response.raise_for_status()
            tags = response.json().get("models", [])
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"models": [], "error": f"Could not reach Ollama at {OLLAMA_URL} ({type(exc).__name__})."},
            status_code=200,
        )
    return {"models": [{"id": m["name"], "sizeMB": round(m.get("size", 0) / 1e6)} for m in tags]}


def allowed_host(host: str) -> bool:
    """Exact host or a subdomain of an allowed one.

    Written as a suffix check with an explicit dot so that "example.com.evil.net" does
    not pass as a subdomain of "example.com" -- the classic way an allowlist like this
    gets walked straight past.
    """
    host = (host or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in API_HOSTS)


def resolves_to_public_address(host: str) -> bool:
    """Whether every address this host resolves to is on the public internet.

    An allowlisted name can still point at 127.0.0.1 or 10.0.0.5, either by accident or
    because someone controls its DNS, and the request would then be made from inside the
    network the server is running in. Checked before the request, not after.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast):
            return False
    return True


@app.get("/fetch")
async def fetch(url: str):
    """Call a public API on the generated page's behalf.

    The page is sandboxed in an iframe and CORS stops it calling most APIs directly, so
    this is the only way a generated app can show real data. Refusals come back as HTTP
    200 with an `error` field, like /models, because the caller is a model-driven UI that
    has to render something sensible rather than crash.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        return {"url": url, "error": "Only https URLs can be fetched."}
    if not allowed_host(parts.hostname or ""):
        return {"url": url, "error": f"{parts.hostname} is not on the allowed API list."}
    if not resolves_to_public_address(parts.hostname):
        return {"url": url, "error": f"{parts.hostname} does not resolve to a public address."}

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
            # No client headers are forwarded and no cookies are kept: the request is
            # anonymous, so nothing the browser holds can leak through it. Redirects are
            # not followed, since a redirect is a way back off the allowlist.
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
    change when the protocol does.
    """
    payload = await request.body()

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
    import json
    return (json.dumps({"error": message}) + "\n").encode()


# Everything else is the app itself. Mounted last so the routes above win.
app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")

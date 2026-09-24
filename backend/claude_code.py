"""Claude, through the Claude Code CLI you are already logged in to.

Each call runs `claude -p` with the app's system prompt in place of Claude Code's own,
no tools, no session saved, and no settings files -- so none of your hooks, plugins or
MCP servers join in. Its streamed events are translated into the NDJSON Ollama speaks,
which is all the engine in frontend/engine.js reads from /chat. The engine does not know
this is not Ollama, and does not need to.

Every call counts against the Claude Code usage limits of whoever is logged in.

    UUI_CLAUDE_BIN     the CLI to run (default: `claude` on PATH; empty turns this off)
    UUI_CLAUDE_MODELS  comma-separated model aliases to offer (default: sonnet,haiku,opus)
"""

import asyncio
import json
import os
import shutil
import tempfile

PREFIX = "claude:"

CLAUDE_BIN = os.environ.get("UUI_CLAUDE_BIN", shutil.which("claude") or "")
MODELS = [
    m.strip() for m in os.environ.get("UUI_CLAUDE_MODELS", "sonnet,haiku,opus").split(",")
    if m.strip()
]

# Run somewhere empty, so no project's CLAUDE.md is read in as context.
WORKDIR = tempfile.mkdtemp(prefix="uui-claude-")

# One line of stream-json can hold a whole reply, so the default 64KB line limit is too
# small for a large region.
LINE_LIMIT = 16 * 1024 * 1024


def available() -> bool:
    return bool(CLAUDE_BIN) and bool(MODELS)


def models() -> list[dict]:
    """For the picker. No size: nothing is downloaded, and nothing sits on the card."""
    if not available():
        return []
    # Metered: every call is spent from a subscription, so the app does not guess ahead
    # with it unless asked.
    return [{"id": PREFIX + m, "sizeMB": None, "metered": True} for m in MODELS]


def is_claude(model: str) -> bool:
    return (model or "").startswith(PREFIX)


def _line(**fields) -> bytes:
    return (json.dumps(fields) + "\n").encode()


async def stream_chat(payload: dict):
    """Run one completion, yielding Ollama-shaped NDJSON lines.

    The process is killed if the caller stops reading -- which is how the app aborts a
    guess the user has overtaken -- so an abandoned reply stops spending tokens at once.
    """
    if not available():
        yield _line(error="Claude Code is not installed on the server's PATH.")
        return

    messages = payload.get("messages", [])
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    max_tokens = (payload.get("options") or {}).get("num_predict")

    env = dict(os.environ)
    # Thinking spends seconds before the first visible token, and the app streams what
    # is written into the page, so the first token is what the user waits on.
    env["MAX_THINKING_TOKENS"] = "0"
    if max_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)

    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p",
        "--model", payload["model"][len(PREFIX):],
        "--system-prompt", system,
        "--output-format", "stream-json", "--include-partial-messages", "--verbose",
        "--tools", "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--settings", json.dumps({"alwaysThinkingEnabled": False}),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKDIR, env=env, limit=LINE_LIMIT,
    )
    try:
        proc.stdin.write(user.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        finished = False
        async for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "stream_event":
                inner = event.get("event") or {}
                delta = inner.get("delta") or {}
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    yield _line(message={"role": "assistant", "content": delta.get("text", "")}, done=False)
            elif kind == "result":
                finished = True
                if event.get("is_error") or event.get("subtype") != "success":
                    reason = event.get("result") or "; ".join(event.get("errors") or []) or event.get("subtype")
                    yield _line(error=f"Claude Code: {reason}")
                else:
                    yield _line(message={"role": "assistant", "content": ""}, done=True)

        await proc.wait()
        if not finished:
            stderr = (await proc.stderr.read()).decode(errors="replace").strip()
            yield _line(error=f"Claude Code exited ({proc.returncode}): {stderr[:400] or 'no output'}")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

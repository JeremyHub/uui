# UUI

An app that generates itself as you use it. Ask it to create a UI for you for a new or
existing app and interact with the application it created. Each page I is generated on the fly
by the model.

Heavily inspired by [Steve Sanderson's Vibe OS](https://www.youtube.com/watch?v=zh6fMtL_cSM)

## How it works

The app owns the document: the head and a shared stylesheet (`frontend/base.css`) go into
a sandboxed iframe before the model is asked anything, and the model writes only body
content, split into `<section data-region="...">` blocks. The first screen streams straight
into the iframe; each click after that sends the model a compacted view of the live screen
plus a short session memory, and it replies with just the regions to replace
(`#plan ...`, `#region <id> ... #end`), which are applied as they stream. Purely local
interactions like tabs and toggles are handled by the page's own scripts and never reach
the model, and a reply can start with `#fetch <url>` to pull in live data before answering.

## How to run it

With Ollama:

```
ollama pull qwen2.5-coder:3b                 # only needed once
uv run uvicorn backend.main:app --reload     # then open http://localhost:8000/
```

With a different Ollama model:

```
OLLAMA_MODEL=llama3.2:3b uv run uvicorn backend.main:app --reload
```

With Claude: if the `claude` CLI is on your PATH and logged in, the model menu also offers
`claude:sonnet`, `claude:haiku` and `claude:opus` (uses your Claude Code subscription).

With no server, in-browser over WebGPU:

```
python3 -m http.server --directory frontend 8080   # then open http://localhost:8080/
```

Run the tests (no GPU or network needed):

```
uv run pytest
```

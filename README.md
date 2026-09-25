# UUI

An app that generates itself as you use it. Ask it to create a UI for you for a new or
existing app and interact with the application it created. Each page I is generated on the fly
by the model.

Heavily inspired by [Steve Sanderson's Vibe OS](https://www.youtube.com/watch?v=zh6fMtL_cSM)

## How it works

1. **You describe an app.** The model writes the first screen, which streams into the page
   as it is generated. The styling comes from the app, so the model only writes content.
2. **You click around.** Each click sends the model what is on screen and what happened so
   far in the session.
3. **Only what changes is rewritten.** The page is split into regions, and the model
   replaces just the ones that need to change, so most clicks take seconds rather than
   a full redraw.

Along the way:

- **Simple interactions stay local.** Tabs, toggles and sorting run in the page itself
  and never wait on the model.
- **It can use real data.** The model can ask the app to fetch an API before it answers,
  so a weather page shows the actual weather.
- **It remembers the session.** Earlier choices are kept in a short summary, so the app
  stays consistent as you go.
- **It runs anywhere.** The model runs on your machine through Ollama (or Claude), or
  entirely in the browser tab with no server.

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

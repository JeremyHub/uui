# uui

FastAPI backend that proxies a running chat transcript to a local Ollama model and
streams back raw HTML, which the frontend injects directly (unsanitized) into the page.
The backend holds no state between requests -- the frontend resends the full transcript
every turn, and it resets on page reload.

Each turn runs two model calls: a **plan** phase that decides what should happen next
(concrete content and interactions, as text) given the timeline/current HTML/browser
state, then a **generate** phase that turns that plan into the actual HTML/CSS/JS
fragment. `/generate` streams both phases back as NDJSON so the frontend can show
progress through each one.

## Run

Ollama needs to be running (on this machine it's a systemd service -- `systemctl status ollama`
-- so you likely don't need to start it yourself) with a model pulled. Default is `gemma3:4b`
-- best content quality of everything tested (see `scripts/test_model.py`), at the cost of
only partial GPU residency on a 4GB card (~30-45s/turn). For lower latency at some quality
cost, `qwen2.5-coder:3b` is fully GPU-resident (~94% GPU) and much faster.

```
ollama pull gemma3:4b     # only needed once

uv run uvicorn backend.main:app --reload
```

Override the model with `OLLAMA_MODEL=<model> uv run ...`. Check `ollama ps` while it's
generating to confirm it's actually running on the GPU (PROCESSOR column).

Then open http://localhost:8000/

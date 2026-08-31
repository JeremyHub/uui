# uui

FastAPI backend that proxies a running chat transcript to a local Ollama model and
streams back raw HTML, which the frontend injects directly (unsanitized) into the page.
The backend holds no state between requests -- the frontend resends the full transcript
every turn, and it resets on page reload.

Each turn runs three model calls: a **plan** phase that decides what should happen next
(concrete content and interactions, as text) given the timeline and a prose summary of
the current screen, a **generate** phase that turns that plan into the actual HTML/CSS/JS
fragment, and a **summary** phase that describes what was just generated. That summary
-- not the raw HTML -- is what the next turn's plan phase sees, since giving the planner
the actual markup made it treat old HTML as material to preserve rather than a screen it's
free to fully replace. `/generate` streams all three phases back as NDJSON so the frontend
can show progress through each one.

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

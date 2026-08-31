# uui

FastAPI backend that proxies a running chat transcript to a local Ollama model and
streams back raw HTML, which the frontend injects directly (unsanitized) into the page.
The backend holds no state between requests -- the frontend resends the full transcript
every turn, and it resets on page reload.

## Run

Ollama needs to be running (on this machine it's a systemd service -- `systemctl status ollama`
-- so you likely don't need to start it yourself) with a model pulled. Pick a model that fits
your VRAM; `qwen2.5-coder:3b` (~2GB) is the default and works well on a 4GB card.

```
ollama pull qwen2.5-coder:3b     # only needed once

uv run uvicorn backend.main:app --reload
```

Override the model with `OLLAMA_MODEL=<model> uv run ...`. Check `ollama ps` while it's
generating to confirm it's actually running on the GPU (PROCESSOR column).

Then open http://localhost:8000/

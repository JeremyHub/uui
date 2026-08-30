# uui

FastAPI backend that proxies a running chat transcript to a local Ollama model and
streams back raw HTML, which the frontend injects directly (unsanitized) into the page.
The backend holds no state between requests -- the frontend resends the full transcript
every turn, and it resets on page reload.

## Run

```
# terminal 1
ollama serve
ollama pull <model>        # e.g. llama3.1, or something smaller/faster

# terminal 2
OLLAMA_MODEL=<model> uv run uvicorn backend.main:app --reload
```

Then open http://localhost:8000/

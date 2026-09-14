# uui

FastAPI backend that turns a running session into live-generated UI from a local Ollama
model. The frontend renders it in a sandboxed iframe, unsanitized, by design. No state is
held between requests — the browser owns the screen and sends what it needs each turn.

## How a turn works

**The first turn** streams one whole HTML document straight into the iframe's parser as
the model writes it, so the page paints top-down while generation is still going. The
model is asked to split the body into `<section data-region="...">` blocks.

**Every turn after that** rewrites only the regions that change. The model replies in a
line-marker format:

```
#plan Show the Siamese photos in the grid
#region photo-grid
<div class="card">...</div>
#end
```

Each region is applied the moment its block finishes, not when the response does.

That is the whole speedup. Output tokens cost ~7x what input tokens do on a local GPU
(measured here: 58 tok/s out vs 414 tok/s in), so writing 300 characters of one region
instead of 4000 characters of a whole document is most of it. Three model calls per turn
also went away: intent, plan and summary are now a single line the patch call emits for
free, plus a screen description the frontend reads straight off the live DOM.

### What never reaches the model

- **Local interactions.** Toggles, tabs, sorting, pagination, dark mode — the generator is
  told to implement these in the page's own `<script>` and mark the control `data-local`.
  Those clicks cost nothing and happen instantly. Models over-apply the attribute, so it
  is treated as a hint: if a `data-local` click changes nothing in the DOM within 400ms,
  the turn is taken after all, and the click is never dead.
- **Predicted clicks.** After a turn settles, the idle GPU generates patches for the most
  likely next clicks. A click that hits one applies in ~0ms. Any real interaction aborts
  the in-flight speculation, which frees the GPU immediately since dropping the connection
  stops Ollama generating.

### What the model sees

Not a prose summary, and not the raw document. `backend/screen.py` sends the real markup
of each region with repeated siblings collapsed — four cards teach the pattern, the
twenty after them only cost tokens. Structure and class names matter: given only text, the
model rewrites styled cards as bare unstyled `<div>`s.

## Run

Ollama needs to be running (a systemd service on this machine — `systemctl status ollama`)
with a model pulled.

```
ollama pull qwen2.5-coder:3b     # only needed once
uv run uvicorn backend.main:app --reload
```

Then open http://localhost:8000/

Default model is `qwen2.5-coder:3b`: fully GPU-resident on a 4GB card and the best patcher
of everything tested. One model serves both phases on purpose — a second model evicts the
first on this card, and the 4-7s reload costs more than any per-phase choice saves.
Override with `OLLAMA_MODEL`, or `UUI_SHELL_MODEL` / `UUI_PATCH_MODEL` per phase if you
have the VRAM. Check `ollama ps` during a turn to confirm the PROCESSOR column says GPU.

## Measuring

```
uv run python3 scripts/bench.py       # server: time per turn, chars written, regions touched
uv run python3 scripts/e2e.py         # browser: what a user actually feels (needs Chrome)
uv run python3 scripts/baseline.py    # the old four-call pipeline, for comparison
```

Same machine, same model (`qwen2.5-coder:3b`, RX 580 4GB):

| | old pipeline | now |
|---|---|---|
| first screen | 37.2s | 17-25s, **first paint ~0.2s** |
| interaction turn | 28.5s mean | **3.4-4.5s mean** (1.5s best, 8.4s worst) |
| local interaction | 28.5s | 0s, no model call |
| predicted click | 28.5s | ~0s |

Turn cost tracks the size of the region being rewritten, so the spread is really a spread
in how well the model split the page up. Asked for 4-7 regions it usually complies, but a
page that comes back as one big region patches like the old design did, because it is the
old design. Region granularity is the thing to watch when a session feels slow.

Run both benchmarks, not one. `bench.py` only exercises the server, and everything that
makes this fast lives in the frontend — streaming into the parser, swapping single
regions, `data-local`, speculation. Three real bugs (a missing function, a frozen status
bar, permanently dead links) were invisible to `bench.py` and obvious in `e2e.py`.

## Layout

```
backend/main.py      /turn: shell and patch streams, the #region parser
backend/prompts.py   the two prompt contracts
backend/screen.py    compacting the live screen into a prompt
frontend/index.html  iframe streaming, region swapping, delegation, speculation
```

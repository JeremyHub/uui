# uui

FastAPI backend that turns a running session into live-generated UI from a local Ollama
model. The frontend renders it in a sandboxed iframe, unsanitized, by design. No state is
held between requests — the browser owns the screen and sends what it needs each turn.

## How a turn works

The app owns the document. Doctype, head and stylesheet are written into the iframe before
the model is asked anything, so the first paint is styled and immediate. The model writes
content and nothing else.

**The first turn** streams body content straight into the iframe's parser as it is
generated, so the page fills in rather than appearing. It is asked to split the body into
`<section data-region="...">` blocks.

**Every turn after that** rewrites only the regions that change. The model replies in a
line-marker format:

```
#plan Show the Siamese photos in the grid
#region photo-grid
<div class="card">...</div>
#end
```

Each region is applied as it is written — cut wherever the tag depth returns to zero, so a
chunk is always renderable on its own and a large region fills in rather than staying
blank until its block closes.

That is the whole speedup. Output tokens cost ~20x what input tokens do on a local GPU
(measured here: 58 tok/s out vs ~1300 tok/s in), so the wins all come from writing less.
Three model calls per turn also went away: intent, plan and summary collapsed into the
single `#plan` line the patch emits anyway, plus a screen description the frontend reads
off the live DOM.

### The stylesheet is the app's, not the model's

`frontend/base.css` is the design system generated pages are written against. The model
used to spend ~350 tokens per first screen writing its own CSS, and every patch prompt
then carried that CSS back so new markup would match it. Both costs are gone, and a 3B
model picks classes far more reliably than it writes typography.

It also makes layout fixable. Handed a class vocabulary the model will still drop five
`<img>` tags into a section with no `.grid` and no `.media`, which used to mean one photo
filling the viewport. Now `:has()` spots the shape and lays it out as a grid — once, for
every app this will ever generate. Apps still differ, through a few theme variables
instead of a whole stylesheet.

### What never reaches the model

- **Local interactions.** Toggles, tabs, sorting, pagination, dark mode — the generator is
  told to implement these in the page's own `<script>` and mark the control `data-local`.
  Those clicks cost nothing. Models over-apply the attribute, so it is treated as a hint:
  if a `data-local` click changes nothing within 400ms the turn is taken after all, and
  the click is never dead.
- **Clicks that were already guessed.** Hovering a control starts generating its patch
  immediately, and idle time after a turn is spent working through the rest. A click that
  hits one applies in ~0ms. Any real interaction aborts the in-flight guess, which frees
  the GPU at once since dropping the connection stops Ollama generating.

### What the app guarantees, rather than asks for

A 3B model follows the region contract most of the time, and the gaps are the difference
between fast and slow, or working and broken. Regions are an addressing scheme the app
owns, so where prompting is unreliable the app enforces it instead:

- A region taking more than half the page has its children promoted to regions in its
  place — otherwise every update rewrites the page, which is the design this replaced.
- Blocks the model left untagged get ids backfilled, so nothing is unreachable.
- A `#screen` reply containing no regions is demoted to a single region rather than
  replacing the body, and never into the region holding the control just clicked — the
  biggest region is usually the nav, and losing the nav is worse than a blank screen.
- A reply with a `#plan` and no `#region` is retried once. It means the click did nothing,
  and it is cheap to retry precisely because failing that way generates almost no tokens.

### What the model sees

Not a prose summary, and not the raw document. `backend/screen.py` sends the real markup
of each region with repeated siblings collapsed — four cards teach the pattern, the twenty
after them only cost tokens. Structure and class names matter: given only text, the model
rewrites styled cards as bare unstyled `<div>`s.

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

## Testing

```
uv run pytest                          # the whole suite, ~50s, no GPU needed
```

The suite talks to `tests/fake_ollama.py`, a stub that speaks Ollama's streaming chat
protocol. There is no test mode and no fake flag in the app — it is the same server,
pointed at something that answers like Ollama. That is what makes the failures testable:
waiting for a real model to emit a truncated reply, a fenced reply, or one that would wipe
the page means waiting for luck, while a stub just emits one.

The browser tests observe from outside — counting POSTs to `/turn`, watching the iframe's
HTML, waiting for both to go quiet. They never read the app's variables. An earlier version
asserted on `busy`, `predictions.size` and `log[]`, which meant renaming a variable broke
the suite and a green suite proved only that those variables still existed. What is
asserted now — *did the screen change, did it cost a model call, how long did it take* —
survives a rewrite, and is what a user would notice.

## Measuring

Correctness is the suite's job; these answer how fast it is with a real model.

```
uv run uvicorn backend.main:app --port 8765 &
uv run python3 scripts/e2e.py         # browser: what a user feels, per turn
uv run python3 scripts/bench.py       # server: chars written, regions touched
uv run python3 scripts/baseline.py    # the old four-call pipeline, for comparison
```

Same machine, same model (`qwen2.5-coder:3b`, RX 580 4GB):

| | old pipeline | now |
|---|---|---|
| first screen | 25-37s | 12-20s, **first paint 0.2-0.5s** |
| interaction turn | 28-31s mean | **2.2-4.1s mean** |
| local interaction | 28-31s | 0s, no model call |
| hovered or guessed click | 28-31s | ~0s |

Turn cost tracks the size of the region being rewritten, so the spread is really a spread
in how well the page was split up. Region granularity is the thing to watch when a session
feels slow.

## Layout

```
backend/main.py       /turn: shell and patch streams, the #region parser
backend/prompts.py    the two prompt contracts
backend/screen.py     compacting the live screen into a prompt
frontend/base.css     the design system generated pages are written against
frontend/index.html   iframe streaming, region swapping, delegation, guessing
tests/fake_ollama.py  a stub that answers like Ollama
tests/browser.py      observing the app from outside: calls made, screen changed
```

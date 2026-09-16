# uui

An app that generates itself as you use it. You say what it should be, a model writes the
first screen, and every click after that rewrites only the parts that change. The output
is rendered unsanitized in a sandboxed iframe, by design.

It runs two ways:

- **with Ollama**, through the small Python server in `backend/`, or
- **entirely in the browser** over WebGPU, in which case `frontend/` is the whole app and
  there is no server at all.

## How a turn works

The app owns the document. Doctype, head and stylesheet go into the iframe before the
model is asked anything, so the first paint is styled and immediate. The model writes
content and nothing else.

**The first turn** streams body content straight into the iframe's parser as it is
generated, so the page fills in rather than appearing. The model is asked to split the
body into `<section data-region="...">` blocks.

**Every turn after that** rewrites only the regions that change:

```
#plan Show the Siamese photos in the grid
#region photo-grid
<div class="card">...</div>
#end
```

Each region is applied as it is written — cut wherever the tag depth returns to zero, so
a chunk always renders on its own and a large region fills in rather than staying blank.

That is the whole speedup. Output tokens cost ~20x what input tokens do on a local GPU
(measured here: 58 tok/s out against ~1300 tok/s in), so every win comes from writing
less. Three model calls per turn also went away: intent, plan and summary collapsed into
the single `#plan` line the patch emits anyway, plus a screen description read off the
live DOM.

### The protocol lives in the browser

`frontend/` holds the prompts, the screen compaction, the reply parser and the turn
orchestration. That is what makes a static copy of the directory a working app, and it
avoids the alternative: two implementations of the protocol, one per language, kept in
step by hand. Compaction got *shorter* in the move — the server had to parse HTML text
with a hand-written parser to walk the screen; the browser has the screen already.

The backend keeps only what a browser cannot do for itself: reach Ollama past CORS, list
what it has pulled, and call an external API on the page's behalf.

### The stylesheet is the app's, not the model's

`frontend/base.css` is the design system generated pages are written against. The model
used to spend ~350 tokens per first screen writing its own CSS, and every patch prompt
then carried it back so new markup would match. Both costs are gone, and a 3B model picks
classes far more reliably than it writes typography.

It also makes layout fixable. Handed a class vocabulary the model will still drop five
`<img>` tags into a section with no `.grid`, which used to mean one photo filling the
viewport. Now `:has()` spots the shape and lays it out as a grid — once, for every app
this will ever generate.

### What never reaches the model

- **Local interactions.** Toggles, tabs, sorting, pagination — the generator implements
  these in the page's own `<script>` and marks the control `data-local`. Those clicks cost
  nothing. Models over-apply the attribute, so it is a hint: if a `data-local` click
  changes nothing within 400ms the turn is taken after all, and the click is never dead.
- **Clicks that were already guessed.** Hovering a control starts generating its patch
  immediately; idle time after a turn works through the rest. A hit applies in ~0ms. Any
  real interaction aborts the in-flight guess, freeing the GPU at once.

### What the app guarantees rather than asks for

A 3B model follows the region contract most of the time, and the gaps are the difference
between fast and slow, or working and broken. Regions are an addressing scheme the app
owns, so where prompting is unreliable the app enforces it:

- a region taking more than half the page has its children promoted to regions in its
  place — otherwise every update rewrites the page, which is the design this replaced
- blocks the model left untagged get ids backfilled, and repeated ids are made unique —
  a repeated id means every patch aimed at it lands on the first copy
- a `#screen` reply containing no regions is demoted to a single region rather than
  replacing the body, and never into the region holding the control just clicked
- a reply with a `#plan` and no `#region` is retried once

### Remembering the session

A turn sees the screen and the click that caused it. Everything the user established
earlier — the name they typed, the filter they chose — is no longer on screen, so without
a record it is gone, and the app starts contradicting itself exactly when a session gets
long enough to be worth having.

Recent turns go into the prompt verbatim; older ones are folded into a few sentences of
prose by the model itself, during the idle time where the guessing already runs. The
prompt stops growing without losing the past.

### Live data

A page of invented cat breeds is fine. A page of today's weather made of plausible
numbers is not. The model can open a reply with `#fetch <url>`, meaning it cannot answer
without data; the app fetches it and asks again with the response attached. The directive
is recognised on the first line and generation is cut off there, so asking costs about ten
tokens rather than a wasted screen.

`GET /fetch` is the one place in this app where model output becomes an outbound request,
so it is bounded on every side: an allowlist of key-free public APIs (`UUI_API_ALLOWLIST`
to change it), https only, no redirects, no forwarded headers or cookies, a resolved-address
check so an allowlisted name cannot point at `127.0.0.1`, and a 64KB cap because the body
ends up in a prompt. Refusals are reported to the model — told the data is unavailable it
says so, given silence it invents the numbers.

## Run it

### With Ollama

```
ollama pull qwen2.5-coder:3b     # only needed once
uv run uvicorn backend.main:app --reload
```

Then open http://localhost:8000/

`qwen2.5-coder:3b` is the default: fully GPU-resident on a 4GB card and the best patcher
of everything tested. Override with `OLLAMA_MODEL`, or pick from the model menu.

### With no server at all

```
python3 -m http.server --directory frontend 8080
```

Open http://localhost:8080/ in a browser with WebGPU. The app notices there is no backend,
offers only in-tab inference, and downloads a model into the tab — gigabytes the first
time, cached afterwards. Any static host works; so does opening `frontend/index.html`
directly.

Model choice defaults to **Auto**, because the honest answer for most people is "whichever
one works" and the wrong choice is a long download that ends in an out-of-memory error.
Auto takes the largest model fitting the device's WebGPU buffer limits, prefers a
coder-tuned one at the same size, and will not pick anything under about a billion
parameters unless nothing else fits — below that a model cannot hold to the reply format.

## Testing

```
uv run pytest        # ~2 minutes, no GPU and no network
```

The suite talks to `tests/fake_ollama.py`, a stub speaking Ollama's streaming protocol.
There is no test mode in the app — it is the same code, pointed at something that answers
like Ollama. That is what makes the failures testable: waiting for a real model to emit a
truncated reply, a fenced reply, or one that would wipe the page means waiting for luck.

The browser tests observe from outside — counting POSTs to the model endpoint, watching
the iframe's HTML, waiting for both to go quiet. They never read the app's variables. An
earlier version asserted on `busy` and `predictions.size`, which meant renaming a variable
broke the suite and a green suite proved only that those variables still existed.

| file | what it covers |
|---|---|
| `tests/test_protocol.py` | compaction, the reply parser, the guards — run in a browser |
| `tests/test_browser.py` | what the app does: clicks, guessing, memory, the overlay |
| `tests/test_fetch.py` | what `/fetch` refuses |
| `tests/test_static.py` | the app served as plain files, with no backend |

## Measuring

```
uv run uvicorn backend.main:app --port 8765 &
uv run python3 scripts/e2e.py         # what a user feels, per turn
uv run python3 scripts/baseline.py    # the original four-call pipeline, for comparison
```

Same machine, same model (`qwen2.5-coder:3b`, RX 580 4GB):

| | original pipeline | now |
|---|---|---|
| first screen | 25-37s | 8-20s, **first paint 0.2-1.9s** |
| interaction turn | 28-31s mean | **2-4s mean** |
| local interaction | 28-31s | 0s, no model call |
| hovered or guessed click | 28-31s | ~0s |

Turn cost tracks the size of the region being rewritten, so the spread is really a spread
in how well the page was split up.

## Layout

```
backend/main.py        serve, proxy Ollama, fetch an allowlisted API
frontend/index.html    the shell: model picker, loading overlay, the iframe
frontend/main.js       wiring: turns, guessing, delegation, the overlay
frontend/turn.js       running a turn, and the guards around a reply
frontend/prompts.js    the two prompt contracts
frontend/parser.js     the #region reply format, parsed as it streams
frontend/screen.js     compacting the live screen into a prompt
frontend/journal.js    what the session remembers
frontend/transports.js where the model runs: Ollama, or this tab
frontend/apis.js       live data, and shrinking it to fit a prompt
frontend/dom.js        reading and changing the generated page
frontend/base.css      the design system generated pages are written against
```

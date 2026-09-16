# UUI

uui is an app to get llms to generate html as the user interacts with it. truly dynamic
websites that present anything the user wants and interact exactly the way the user wants
them to.

# Guidelines

- commit directly to main, dont use worktrees
- speed is a feature. the model rewrites regions, not pages -- keep it that way
- output tokens cost ~20x input tokens locally. optimise what the model writes, not what
  it reads; measure before assuming a prompt is the problem
- prompts leak: naming example region ids got them rendered as visible nav links, and
  naming `cart`/`score` got a cat gallery a cart. describe, don't exemplify
- a 3B model follows the contract most of the time. where the gap is the difference
  between fast and slow, or working and broken, have the app guarantee it rather than
  asking again in the prompt -- regions and their ids belong to the app
- `uv run pytest` is fast and deterministic (stubbed model, no GPU). the browser tests
  observe from outside -- calls to /turn, the iframe's HTML -- and must stay that way;
  reading the app's variables makes a green suite mean nothing
- `scripts/e2e.py` is the user-facing measurement. `scripts/bench.py` only sees the
  server and reads pessimistic, because the frontend normalises the page between turns

# UUI

uui is an app to get llms to generate html as the user interacts with it. truly dynamic
websites that present anything the user wants and interact exactly the way the user wants
them to.

# Guidelines

- commit directly to main, dont use worktrees
- speed is a feature. the model rewrites regions, not pages -- keep it that way
- output tokens cost ~20x input tokens locally. optimise what the model writes, not what
  it reads; measure before assuming a prompt is the problem
- the protocol lives in `frontend/`, not the backend, so a static copy of that directory
  is a working app. do not move any of it back to Python -- two implementations of the
  same protocol is the thing that arrangement exists to avoid
- prompts leak, and position matters: naming example region ids got them rendered as
  visible nav links, and appending an API catalogue to the end of the shell prompt pushed
  the interactivity rules into the middle, where a 3B model stopped following them
- a 3B model follows the contract most of the time. where the gap is the difference
  between fast and slow, or working and broken, have the app guarantee it rather than
  asking again in the prompt -- regions and their ids belong to the app
- `uv run pytest` is fast and deterministic (stubbed model, no GPU, no network). the
  browser tests observe from outside -- calls to the model endpoint, the iframe's HTML --
  and must stay that way; reading the app's variables makes a green suite mean nothing
- `scripts/e2e.py` is the user-facing measurement, and worth running against a real model
  before claiming a change works

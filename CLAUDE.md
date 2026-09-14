# UUI

uui is an app to get llms to generate html as the user interacts with it. truly dynamic
websites that present anything the user wants and interact exactly the way the user wants
them to.

# Guidelines

- commit directly to main, dont use worktrees
- speed is a feature. the model rewrites regions, not pages -- keep it that way
- prompts leak: naming example region ids in a prompt got them rendered as visible nav
  links, and naming `cart`/`score` got a cat gallery a cart. describe, don't exemplify
- run `scripts/e2e.py` (real browser) as well as `scripts/bench.py` (server) before
  claiming something works -- most of this app is frontend, and bench.py cannot see it

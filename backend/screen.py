"""Compact the live screen into something worth putting in a prompt.

The patch model has to write markup that drops seamlessly into a page it cannot see.
Given only a text summary it writes generic unstyled <div>s, because it has no idea the
page uses .card, or that results are a grid of <img>+<h2>+<p>. Given the raw HTML it
writes good markup, but a real page is mostly repetition -- six identical cards, twenty
identical rows -- and that repetition is what makes the prompt expensive.

So: keep the structure, drop the repetition. The first couple of siblings of a kind
teach the pattern; the rest only cost tokens. On this hardware input runs ~7x faster
than output, so a structurally faithful prompt is cheap and the quality it buys is the
whole point.
"""

import re
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}
# Attributes that tell the model something about how to write matching markup.
KEEP_ATTRS = {"class", "id", "href", "src", "alt", "type", "name", "value",
              "placeholder", "checked", "selected", "disabled", "role", "colspan"}

MAX_TEXT = 200
MAX_SCRIPT = 400
MAX_STYLE_BLOCK = 1600
# Below this, a run of siblings is cheap enough to send verbatim. Eliding a 3-item list
# saves almost nothing and costs a lot: small models copy the elision marker straight
# back into their answer, so every elision is a chance to corrupt the output.
MIN_RUN = 6
DEFAULT_KEEP = 4

def elision(n: int, tag: str) -> str:
    return f"<!-- and {n} more <{tag}> like those above: WRITE THEM ALL OUT IN FULL -->"


# Matches anything shaped like an elision marker, ours or a mangled copy of one, so the
# server can guarantee no marker ever reaches the page.
ELISION_RE = re.compile(r"<!--(?:(?!-->).)*?\bmore\b(?:(?!-->).)*?-->",
                        re.IGNORECASE | re.DOTALL)


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []


class Text:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


class TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].children.append(Text(" ".join(data.split())))


def parse(html: str) -> Node:
    p = TreeParser()
    p.feed(html)
    p.close()
    return p.root


def signature(node: Node) -> str:
    """What makes two siblings 'the same kind of thing' -- tag plus class."""
    return f"{node.tag}.{node.attrs.get('class', '')}"


def serialize(node, out, keep, depth=0):
    if isinstance(node, Text):
        text = node.text
        out.append(text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…")
        return

    attrs = "".join(
        f' {k}="{v}"' if v else f" {k}"
        for k, v in node.attrs.items()
        if k in KEEP_ATTRS or k.startswith("data-") or k.startswith("aria-")
    )
    out.append(f"<{node.tag}{attrs}>")
    if node.tag in VOID:
        return

    if node.tag in ("script", "style"):
        body = "".join(c.text for c in node.children if isinstance(c, Text))
        limit = MAX_SCRIPT if node.tag == "script" else MAX_STYLE_BLOCK
        out.append(body if len(body) <= limit else body[:limit] + "\n/* ... */")
        out.append(f"</{node.tag}>")
        return

    serialize_children(node.children, out, keep, depth + 1)
    out.append(f"</{node.tag}>")


def serialize_children(children, out, keep, depth=0):
    """Collapse runs of same-kind siblings: the first few show the pattern, a count
    stands in for the rest so the model still knows how much content is really there."""
    run_sig, run_n = None, 0
    for child in children:
        if isinstance(child, Node):
            sig = signature(child)
            if sig == run_sig:
                run_n += 1
                if run_n > keep:
                    continue
            else:
                flush_run(out, run_sig, run_n, keep)
                run_sig, run_n = sig, 1
        serialize(child, out, keep, depth)
    flush_run(out, run_sig, run_n, keep)


def flush_run(out, run_sig, run_n, keep):
    if run_sig is None or run_n < MIN_RUN or run_n <= keep:
        return
    out.append(elision(run_n - keep, run_sig.split(".")[0]))


def compact(html: str, keep: int = DEFAULT_KEEP) -> str:
    out = []
    serialize_children(parse(html).children, out, keep)
    return "".join(out)


def render_screen(regions: list[dict], styles: str = "", budget: int = 7000) -> str:
    """Render the addressable screen for the prompt, inside a character budget.

    Over budget, drop to one example per repeated group before truncating anything --
    losing the fourth identical card costs the model nothing, losing the tail of a
    region costs it the structure it was about to imitate.
    """
    def build(keep):
        parts = []
        if styles:
            css = styles if len(styles) <= MAX_STYLE_BLOCK else styles[:MAX_STYLE_BLOCK] + "\n/* ... */"
            parts.append(f"<style>\n{css}\n</style>")
        for r in regions:
            parts.append(
                f'<section data-region="{r["id"]}">\n{compact(r.get("html", ""), keep)}\n</section>'
            )
        return "\n".join(parts)

    for keep in (DEFAULT_KEEP, 2, 1):
        text = build(keep)
        if len(text) <= budget:
            return text
    return text[:budget] + "\n<!-- ...screen truncated -->"


class RegionFinder(HTMLParser):
    """Locate [data-region] elements and hand back their exact inner HTML.

    Offset-based rather than re-serialized: the benchmark feeds these regions straight
    back into the next turn, so anything lossy here would quietly make the benchmark
    measure a different page than the browser does.
    """

    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.line_starts = [0]
        for i, ch in enumerate(html):
            if ch == "\n":
                self.line_starts.append(i + 1)
        self.open = []          # (tag, region_id_or_None, inner_start)
        self.regions = []

    def _offset(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        rid = dict(attrs).get("data-region")
        end = self.html.find(">", self._offset())
        self.open.append((tag, rid, end + 1))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                _, rid, start = self.open[i]
                if rid:
                    self.regions.append({"id": rid, "html": self.html[start:self._offset()]})
                del self.open[i:]
                return


def find_regions(html: str) -> list[dict]:
    p = RegionFinder(html)
    p.feed(html)
    p.close()
    # Document order, not close order -- a nested region closes before its parent.
    return sorted(p.regions, key=lambda r: html.find(f'data-region="{r["id"]}"'))

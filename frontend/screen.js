// Compacting the live screen into something worth putting in a prompt.
//
// The patch model has to write markup that drops seamlessly into a page it cannot see.
// Given only a text summary it writes generic unstyled <div>s, because it has no idea the
// page uses .card, or that results are a grid of <img>+<h2>+<p>. Given the raw HTML it
// writes good markup, but a real page is mostly repetition -- six identical cards, twenty
// identical rows -- and that repetition is what makes the prompt expensive.
//
// So: keep the structure, drop the repetition. The first few siblings of a kind teach the
// pattern; the rest only cost tokens.
//
// This walks the real DOM. The server-side version parsed HTML text with a hand-written
// parser to do the same job, which is most of what made it long.

const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
                      "link", "meta", "param", "source", "track", "wbr"]);
// Attributes that tell the model something about how to write matching markup.
const KEEP_ATTRS = new Set(["class", "id", "href", "src", "alt", "type", "name", "value",
                            "placeholder", "checked", "selected", "disabled", "role", "colspan"]);

const MAX_TEXT = 200;
const MAX_SCRIPT = 400;
const MAX_STYLE_BLOCK = 1600;
// Below this, a run of siblings is cheap enough to send verbatim. Eliding a 3-item list
// saves almost nothing and costs a lot: small models copy the elision marker straight
// back into their answer, so every elision is a chance to corrupt the output.
const MIN_RUN = 6;
const DEFAULT_KEEP = 4;

export const elision = (n, tag) =>
  `<!-- and ${n} more <${tag}> like those above: WRITE THEM ALL OUT IN FULL -->`;

// Matches anything shaped like an elision marker, ours or a mangled copy of one, so the
// app can guarantee no marker ever reaches the page.
export const ELISION_RE = /<!--(?:(?!-->)[\s\S])*?\bmore\b(?:(?!-->)[\s\S])*?-->/gi;

const signature = (el) => `${el.tagName.toLowerCase()}.${el.getAttribute("class") || ""}`;

function truncate(text, limit) {
  return text.length <= limit ? text : text.slice(0, limit) + "…";
}

function serialize(node, out, keep, root) {
  if (node.nodeType === Node.TEXT_NODE) {
    const text = node.textContent.replace(/\s+/g, " ").trim();
    if (text) out.push(truncate(text, MAX_TEXT));
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return;

  // A region nested inside another is sent separately, so repeating it here would put
  // the same content in the prompt twice and invite a patch that clobbers the child.
  if (node !== root && node.hasAttribute("data-region")) {
    out.push(`<!-- region ${node.getAttribute("data-region")}, sent separately -->`);
    return;
  }

  const tag = node.tagName.toLowerCase();
  let attrs = "";
  for (const { name, value } of node.attributes) {
    if (KEEP_ATTRS.has(name) || name.startsWith("data-") || name.startsWith("aria-")) {
      attrs += value ? ` ${name}="${value}"` : ` ${name}`;
    }
  }
  out.push(`<${tag}${attrs}>`);
  if (VOID.has(tag)) return;

  if (tag === "script" || tag === "style") {
    out.push(truncate(node.textContent, tag === "script" ? MAX_SCRIPT : MAX_STYLE_BLOCK));
    out.push(`</${tag}>`);
    return;
  }
  serializeChildren(node.childNodes, out, keep, root);
  out.push(`</${tag}>`);
}

// Collapse runs of same-kind siblings: the first few show the pattern, a count stands in
// for the rest so the model still knows how much content is really there.
function serializeChildren(nodes, out, keep, root) {
  let runSig = null, runN = 0;

  const flushRun = () => {
    if (runSig !== null && runN >= MIN_RUN && runN > keep) {
      out.push(elision(runN - keep, runSig.split(".")[0]));
    }
  };

  for (const child of nodes) {
    if (child.nodeType === Node.ELEMENT_NODE) {
      const sig = signature(child);
      if (sig === runSig) {
        runN += 1;
        if (runN > keep) continue;
      } else {
        flushRun();
        runSig = sig;
        runN = 1;
      }
    }
    serialize(child, out, keep, root);
  }
  flushRun();
}

export function compact(el, keep = DEFAULT_KEEP) {
  const out = [];
  serializeChildren(el.childNodes, out, keep, el);
  return out.join("");
}

/** The regions on screen, outermost first, as the model will be asked to address them. */
export function regionsOf(doc) {
  return [...doc.querySelectorAll("[data-region]")];
}

/**
 * Render the addressable screen for the prompt, inside a character budget.
 *
 * Over budget, drop to one example per repeated group before truncating anything --
 * losing the fourth identical card costs the model nothing, losing the tail of a region
 * costs it the structure it was about to imitate.
 */
export function renderScreen(doc, budget = 7000) {
  const themeStyles = [...doc.querySelectorAll("style:not([data-uui-base])")]
    .map((el) => el.textContent).join("\n").trim();

  const build = (keep) => {
    const parts = [];
    if (themeStyles) parts.push(`<style>\n${truncate(themeStyles, MAX_STYLE_BLOCK)}\n</style>`);
    for (const region of regionsOf(doc)) {
      const id = region.getAttribute("data-region");
      parts.push(`<section data-region="${id}">\n${compact(region, keep)}\n</section>`);
    }
    return parts.join("\n");
  };

  for (const keep of [DEFAULT_KEEP, 2, 1]) {
    const text = build(keep);
    if (text.length <= budget) return text;
  }
  return build(1).slice(0, budget) + "\n<!-- ...screen truncated -->";
}

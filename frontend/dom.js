// Reading and changing the live screen.
//
// Everything here works on the generated document inside the iframe: finding what the
// user can click, keeping regions addressable, and applying the model's output to them.
// It is deliberately separate from the turn protocol -- this is about the page, not
// about the model.



export function errorRegion(message) {
  return '<section data-region="uui-error"><div class="panel"><h3>Something went wrong</h3>' +
         `<p class="muted">${String(message).replace(/[<&]/g, "")}</p></div></section>`;
}

export function runScripts(root, doc) {
  root.querySelectorAll("script:not([data-uui-ran])").forEach((old) => {
    const s = doc.createElement("script");
    for (const a of old.attributes) s.setAttribute(a.name, a.value);
    // A region arrives in pieces, so this runs repeatedly over the same element as it
    // fills; without a mark, every script in it would execute once per piece.
    s.setAttribute("data-uui-ran", "");
    // Wrapped so its declarations are its own. Scripts get re-run here -- after a
    // region is replaced, or to repair one that threw mid-parse -- and running `const
    // input = ...` a second time is a syntax error that kills the whole script. Two
    // regions each declaring `items` would collide the same way. The cost is that a
    // function defined here is no longer global, so an inline onclick="foo()" stops
    // resolving; those clicks fall through to a model turn, which is the safe direction.
    s.textContent = `(function(){\n${old.textContent}\n})();`;
    old.replaceWith(s);
  });
}

// Models reliably tag most blocks and then quietly skip one -- a bare <header> whose
// id they only mention in a CSS selector -- and they habitually wrap the whole page in
// a single <div class="container">. Either leaves content sitting outside any region,
// where it can never be updated. So: find the element whose children are the real
// top-level blocks, then backfill an id onto every one of them that lacks it.
// Which element's children are the real top-level blocks.
//
// If the model tagged regions at all, believe it: the host is wherever it put the most
// of them, even if that is a couple of wrappers deep. Picking a different level would
// strip its ids and replace good names like "recipe-list" with "block-2". Only when
// there are no regions at all does this fall back to descending through wrapper divs.
export function regionHost(doc) {
  const tagged = [...doc.querySelectorAll("[data-region]")];
  if (tagged.length) {
    const counts = new Map();
    for (const el of tagged) {
      if (el.parentElement) counts.set(el.parentElement, (counts.get(el.parentElement) || 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1])[0][0];
  }
  let host = doc.body;
  while (true) {
    const kids = [...host.children].filter((c) => !["SCRIPT", "STYLE"].includes(c.tagName));
    if (kids.length !== 1 || !kids[0].children.length) return host;
    host = kids[0];
  }
}

export function ensureRegions(doc) {
  const host = regionHost(doc);

  // Ids address regions, so duplicates are not a cosmetic problem: every patch aimed at
  // a repeated id lands on the first one, and the rest can never be updated at all.
  // Models emit the same id five times more often than you would hope.
  const taken = new Set();
  doc.querySelectorAll("[data-region]").forEach((el) => {
    let id = el.getAttribute("data-region");
    if (taken.has(id)) {
      let n = 2;
      while (taken.has(`${id}-${n}`)) n++;
      id = `${id}-${n}`;
      el.setAttribute("data-region", id);
    }
    taken.add(id);
  });

  // Regions are top-level blocks, full stop. Told to tag "every top-level block", models
  // will happily tag every recipe card and every card's info panel too -- one page came
  // back with thirty regions, which puts the whole document in the prompt again and
  // undoes the entire point. Only direct children of the host stay addressable.
  [...doc.querySelectorAll("[data-region]")].forEach((el) => {
    if (el.parentElement !== host) el.removeAttribute("data-region");
  });

  const used = taken;

  // Tagging a block that already holds regions would nest them, and a nested region is
  // worse than no region: its parent carries a duplicate of everything inside it, so
  // every prompt pays for the same content twice and patching the parent silently
  // destroys the children's ids. Descend into such a block and tag its children instead.
  (function backfill(parent) {
    [...parent.children].forEach((child, i) => {
      if (child.tagName === "SCRIPT" || child.tagName === "STYLE") return;
      if (child.hasAttribute("data-region")) return;
      const guess = { HEADER: "header", NAV: "nav", FOOTER: "footer", MAIN: "main",
                      ASIDE: "sidebar", FORM: "form" }[child.tagName] || `block-${i + 1}`;
      let name = guess, n = 2;
      while (used.has(name)) name = `${guess}-${n++}`;
      used.add(name);
      child.setAttribute("data-region", name);
    });
  })(host);
}

// Region granularity is what an update costs: patching rewrites one region, so a page
// that comes back as a single region patches like the old whole-page design did. Asked
// for 4-7 regions a 3B model complies most of the time and then returns one <section>
// wrapping everything, and no amount of prompting reliably fixes that.
//
// It does not have to. Regions are an addressing scheme the app owns, so the app can
// enforce the granularity it needs: anything that dominates the page gets its children
// promoted to regions in its place. Nothing moves in the DOM -- the oversized block
// stays exactly where it is as a layout container, it just stops being the unit of
// update.
const OVERSIZED_SHARE = 0.55;
const MAX_REGIONS = 8;

export function regionName(el, used) {
  const heading = el.querySelector("h1, h2, h3, h4");
  const raw = (heading ? heading.textContent : el.getAttribute("aria-label") || "")
    .trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 28);
  // Falling back to the tag name gives regions called "h2" and "div", which tell the
  // model nothing about what it is being asked to rewrite.
  const base = raw || (el.className || "").trim().split(/\s+/)[0] || "part";
  let name = base, n = 2;
  while (used.has(name)) name = `${base}-${n++}`;
  used.add(name);
  return name;
}

export function splitOversizedRegions(doc) {
  const total = doc.body.innerHTML.length || 1;
  const used = new Set(
    [...doc.querySelectorAll("[data-region]")].map((el) => el.getAttribute("data-region"))
  );
  for (const region of [...doc.querySelectorAll("[data-region]")]) {
    if (doc.querySelectorAll("[data-region]").length >= MAX_REGIONS) break;
    if (region.innerHTML.length / total < OVERSIZED_SHARE) continue;
    const children = [...region.children].filter((c) => !["SCRIPT", "STYLE"].includes(c.tagName));
    const kids = children.filter((c) => c.textContent.trim() && !c.matches(FIELDS));
    // Whatever is not promoted ends up in no region at all: missing from the screen the
    // model reads, and out of reach of any patch. An empty spacer can go. A search box
    // cannot -- an <input> has no text, and splitting its region around it meant the
    // model never saw what was typed there, or that there was a box at all.
    const stranded = children.filter((c) => !kids.includes(c) &&
      (c.matches(`${FIELDS}, img`) || c.querySelector(`${FIELDS}, img`)));
    if (kids.length < 2 || stranded.length) continue;
    used.delete(region.getAttribute("data-region"));
    region.removeAttribute("data-region");
    kids.forEach((kid) => {
      if (!kid.hasAttribute("data-region")) kid.setAttribute("data-region", regionName(kid, used));
    });
  }
}

// Every small model tested invents local image paths ("cat1.jpg") no matter how firmly
// the prompt forbids it, and a page full of broken-image icons reads as broken even when
// the content is good. Repair it in the DOM instead of relying on the model: seed a real
// photo from the alt text, so the picture is at least about the right subject.

export function repairImages(root, doc) {
  root.querySelectorAll("img").forEach((img) => {
    const fix = () => {
      const seed = encodeURIComponent(
        (img.alt || img.getAttribute("src") || "photo").split("/").pop()
          .replace(/\.[a-z]+$/i, "").slice(0, 40) || "photo"
      );
      const replacement = `https://picsum.photos/seed/${seed}/600/400`;
      if (img.getAttribute("src") !== replacement) img.src = replacement;
    };
    // "seed/SLUG" is the literal example out of the prompt, copied verbatim -- it loads,
    // but it is then the same photo for every such image, which reads as obviously wrong.
    const src = img.getAttribute("src") || "";
    if (!/^https?:/i.test(src) || /\/seed\/SLUG\b/.test(src)) fix();
    else img.addEventListener("error", fix, { once: true });
  });
}

export function flash(el) {
  el.animate(
    [{ opacity: 0.35, transform: "translateY(3px)" }, { opacity: 1, transform: "none" }],
    { duration: 220, easing: "ease-out" }
  );
}

export function regionTarget(doc, id) {
  let target = doc.querySelector(`[data-region="${CSS.escape(id)}"]`);
  if (!target) {
    target = doc.createElement("section");
    target.setAttribute("data-region", id);
    (doc.querySelector("main") || doc.body).append(target);
  }
  return target;
}

// A region arrives as a run of complete elements rather than one finished block, so it
// fills in as the model writes it instead of staying blank until the block closes.
export function openRegion(doc, id) {
  const target = regionTarget(doc, id);
  target.innerHTML = "";
  flash(target);
}

export function appendToRegion(doc, id, html) {
  const target = regionTarget(doc, id);
  target.insertAdjacentHTML("beforeend", html);
  repairImages(target, doc);
  runScripts(target, doc);
}

export function applyRegion(doc, id, html) {
  const target = regionTarget(doc, id);
  if (target.innerHTML === html) return;   // already delivered in pieces
  target.innerHTML = html;
  repairImages(target, doc);
  runScripts(target, doc);
  flash(target);
}

export function applyScreen(doc, html) {
  doc.body.innerHTML = html;
  repairImages(doc.body, doc);
  runScripts(doc.body, doc);
  ensureRegions(doc);
  splitOversizedRegions(doc);
  flash(doc.body);
}

// --- talking to the backend -------------------------------------------------

async function* streamTurn(payload, signal) {
  const res = await fetch("/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) !== -1) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line) yield JSON.parse(line);
    }
  }
}

// --- speculation ------------------------------------------------------------
//
// Ollama serializes work on one GPU, so a speculation that is still running would
// delay a real click. Every speculation therefore gets an AbortController and is
// killed the instant the user actually does something -- closing the connection
// stops Ollama generating, so the GPU is free again immediately.

// --- what the user has done to the page ---------------------------------------
//
// Everything a person can change on a page: every field, named or not, and anything the
// page marks as toggled. Generated pages rarely name their controls -- a search box is
// <input placeholder="Search"> with neither name nor id -- so keying on names alone
// dropped exactly the thing that was typed.

export const FIELDS = 'input, select, textarea, [contenteditable=""], [contenteditable="true"]';
// Their value is a label, not something the user entered.
const NOT_INPUT = /^(submit|button|reset|image|file)$/;

/** Never sent to a model. The length is enough to know something was entered. */
export const maskSecret = (value) => (value ? `(${value.length} characters, hidden)` : "");

/** What the user has put in a field, read from the field rather than its markup. */
export function fieldValue(field) {
  if (field.isContentEditable && !/^(INPUT|TEXTAREA|SELECT)$/.test(field.tagName)) {
    return field.innerText.trim();
  }
  if (field.type === "checkbox") return field.checked;
  if (field.type === "password") return maskSecret(field.value);
  if (field.tagName === "SELECT") {
    const chosen = [...field.selectedOptions].map((o) => o.value || o.textContent.trim());
    return field.multiple ? chosen : (chosen[0] ?? "");
  }
  return field.value;
}

/**
 * What to call a field: its name or id when the page gave one, or else what a person
 * would call it -- its label, placeholder or aria-label -- so "Search the web" arrives as
 * the key rather than the field not arriving at all.
 */
export function fieldKey(field) {
  const label = field.labels?.[0]?.textContent ?? field.closest("label")?.textContent;
  const said = field.getAttribute("aria-label") || field.getAttribute("placeholder") ||
    label?.replace(/\s+/g, " ").trim() || field.getAttribute("title");
  if (field.name || field.id || said) return field.name || field.id || said;
  const region = field.closest("[data-region]")?.getAttribute("data-region");
  const kind = field.type || field.tagName.toLowerCase();
  return region ? `${kind} in ${region}` : kind;
}

export function collectFormState(container) {
  const state = {};
  const claim = (key) => {
    let unique = key, n = 2;
    while (unique in state) unique = `${key} (${n++})`;
    return unique;
  };
  container.querySelectorAll(FIELDS).forEach((field) => {
    if (field.tagName === "INPUT" && NOT_INPUT.test(field.type)) return;
    if (field.type === "radio") {
      // A group shares one name and one answer, which is the button that is checked.
      const key = field.name || fieldKey(field);
      if (field.checked) state[key] = field.value === "on" ? fieldKey(field) : field.value;
      else if (!(key in state)) state[key] = null;
      return;
    }
    state[claim(fieldKey(field))] = fieldValue(field);
  });
  // Toggles the page tracks itself, which have no value to read.
  container.querySelectorAll('[aria-pressed], [aria-checked]:not(input), [aria-selected="true"]')
    .forEach((el) => {
      const key = el.name || el.id || el.getAttribute("aria-label") || el.textContent.trim().slice(0, 40);
      if (!key) return;
      const flag = el.getAttribute("aria-pressed") ?? el.getAttribute("aria-checked") ??
        el.getAttribute("aria-selected");
      state[claim(key)] = flag === "true";
    });
  return state;
}

export function describeElement(el) {
  const data = { ...el.dataset };
  const region = el.closest("[data-region]");
  return {
    tag: el.tagName.toLowerCase(),
    text: el.textContent.trim().slice(0, 200),
    id: el.id || undefined,
    name: el.name || undefined,
    href: el.tagName === "A" ? el.getAttribute("href") : undefined,
    type: el.type || undefined,
    inRegion: region ? region.getAttribute("data-region") : undefined,
    // A field has no text of its own, so without these a submit from a bare search box
    // tells the model only that "an input" was submitted.
    ...(el.matches(FIELDS) ? { label: fieldKey(el), value: fieldValue(el) } : {}),
    ...(Object.keys(data).length ? { data } : {}),
  };
}

/** Whether Enter in this element means "go": a one-line field. */
export function submitsOnEnter(el) {
  return el.tagName === "INPUT" && !NOT_INPUT.test(el.type) && !/^(checkbox|radio|range|color)$/.test(el.type);
}

// --- what the user pressed, and where ----------------------------------------------
//
// A field's value says where the user ended up, not how they got there: that they typed
// "chiken", went back and fixed it, then pressed Enter; that they tabbed out of the name
// box into the search box. So every key pressed in a field is kept, in order, against the
// field it was pressed in, and goes to the model with the next turn. What the model does
// with it is its call -- including deciding that nothing on screen needs to change.
//
// Kept as one string of keys per stretch of typing in one field, because a JSON object
// per key press would be the biggest thing in the prompt.

const KEY_SYMBOLS = {
  Backspace: "⌫", Delete: "⌦", Enter: "⏎", Tab: "⇥", Escape: "⎋",
  ArrowLeft: "←", ArrowRight: "→", ArrowUp: "↑", ArrowDown: "↓",
};
const MAX_KEYS = 240;
const MAX_STRETCHES = 12;

/** How one key press is written down, or null for one that says nothing on its own. */
export function keySymbol(e, field) {
  // Pasting is recorded from the paste itself, which carries the text; the keys don't.
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return null;
  if (e.ctrlKey || e.metaKey) return e.key.length === 1 ? `⌃${e.key.toLowerCase()}` : null;
  if (e.key.length === 1) return field.type === "password" ? "•" : e.key;
  return KEY_SYMBOLS[e.key] ?? null;
}

export class KeyLog {
  constructor() {
    this.stretches = [];   // { in, region, keys } oldest first
    this.field = null;     // the field the last stretch belongs to
    this.sent = 0;         // how many stretches the turn in flight has taken
  }

  #stretch(field) {
    const last = this.stretches.at(-1);
    // A turn took the last stretch, so later keys start a new one even in the same field:
    // they are what happened after the model was asked.
    if (last && this.field === field && this.stretches.length > this.sent) return last;
    const region = field.closest("[data-region]")?.getAttribute("data-region");
    const stretch = { in: fieldKey(field), ...(region ? { region } : {}), keys: "" };
    this.stretches.push(stretch);
    this.field = field;
    if (this.stretches.length > MAX_STRETCHES) {
      this.stretches.shift();
      this.sent = Math.max(0, this.sent - 1);
    }
    return stretch;
  }

  key(field, symbol) {
    const stretch = this.#stretch(field);
    stretch.keys += symbol;
    if (stretch.keys.length > MAX_KEYS) stretch.keys = "…" + stretch.keys.slice(-MAX_KEYS);
  }

  paste(field, text) {
    this.key(field, field.type === "password" ? "«pasted»" : `«pasted ${JSON.stringify(text.slice(0, 80))}»`);
  }

  /** Everything since the last turn that went through, for the turn about to be sent. */
  take() {
    this.sent = this.stretches.length;
    return this.stretches.map((s) => ({ ...s }));
  }

  /** The turn that took them finished, so the model has seen them. */
  consumed(count) {
    this.stretches.splice(0, count);
    this.sent = Math.max(0, this.sent - count);
    if (!this.stretches.length) this.field = null;
  }

  /** The turn that took them was abandoned; the next one takes them again. */
  returned() {
    this.sent = 0;
  }
}

/**
 * Whether changing this field is itself a request: a filter dropdown, a toggle. Not when
 * the field is one part of something with its own go button -- ticking "safe search"
 * next to a Search button is setting up the search, not running it, and asking the model
 * then would spend a turn on every box ticked.
 */
export function changeIsRequest(field) {
  if (field.form) return false;
  if (field.tagName === "INPUT" && !/^(checkbox|radio|range|color|date|time|datetime-local|month|week)$/.test(field.type)) {
    return false;
  }
  if (field.tagName === "TEXTAREA") return false;
  const scope = field.closest("[data-region]") ?? field.ownerDocument.body;
  return !scope.querySelector('button, input[type="submit"], input[type="button"], [role="button"]');
}

// --- applying model output --------------------------------------------------

// innerHTML never runs <script>, so scripts in a freshly patched region are re-created
// as real nodes. Without this, a region that ships its own local interactivity would
// render but sit dead.

export const LOCAL_GRACE_MS = 400;

export const REAL_CONTROLS =
  'button, a[href], input[type="submit"], input[type="button"], input[type="reset"], [role="button"]';

// Models do not limit themselves to <button> and <a href>. They write navs out of bare
// <a> with no href, tabs out of <div class="tab">, rows out of <li> -- and style them as
// clickable. Matching only real controls left every one of those permanently dead.
//
// But "any <a> or <li>" is too greedy the other way: a plain <li> in a summary list is
// not a control, and treating it as one spends a model call on a click that meant
// nothing. So the test is whether the page itself declares the thing clickable, either
// with an explicit handler-ish attribute or with cursor:pointer in its own CSS.
const CLICK_INTENT = "[onclick], [tabindex], [data-action], [data-tab], [data-filter]";
// Anything a generated page might plausibly have meant as a control. Deliberately loose.
const CONTROL_SELECTOR = `${REAL_CONTROLS}, ${CLICK_INTENT}, a, li,` +
  ' [class*="tab"], [class*="btn"], [class*="button"], [class*="card"],' +
  ' [class*="item"], [class*="chip"], [class*="nav"], [class*="option"]';

export function allControls(doc) {
  const all = [...doc.querySelectorAll(CONTROL_SELECTOR)]
    .filter((el) => el.textContent.trim() && el.getClientRects().length);
  // Keep only leaf-most matches. A tab strip is usually <div class="tab-bar"> holding
  // <div class="tab">, and the loose selector matches both -- but the container is not
  // what anyone clicks, and offering it as a target means predicting a click whose
  // "label" is every tab's text run together.
  return all.filter((el) => !all.some((other) => other !== el && el.contains(other)));
}

// Whether the page itself says this is clickable -- a real control, an explicit handler
// attribute, or cursor:pointer in its own CSS. Stricter than CONTROL_SELECTOR.
export function isClickable(el, doc) {
  if (el.matches(REAL_CONTROLS) || el.matches(CLICK_INTENT)) return true;
  return doc.defaultView.getComputedStyle(el).cursor === "pointer";
}

// The two callers want opposite things, so they get opposite thresholds.
//
// A click already happened: the user believed that thing was clickable, and a wasted
// turn is far better than a control that does nothing forever. So accept loosely.
// Speculation is a guess spending GPU nobody asked for, so it accepts strictly
// (isClickable) -- otherwise it burns turns predicting clicks on summary list rows.
export function findControl(target, doc) {
  const direct = target.closest(REAL_CONTROLS) || target.closest(CLICK_INTENT);
  if (direct) return direct;
  let el = target;
  for (let depth = 0; el && el !== doc.body && depth < 4; el = el.parentElement, depth++) {
    if (/^(INPUT|TEXTAREA|SELECT|LABEL|OPTION)$/.test(el.tagName)) return null;
    if (el.matches(CONTROL_SELECTOR) || isClickable(el, doc)) return el;
  }
  return null;
}

export function isLocal(el) {
  return el.hasAttribute("data-local") || !!el.closest("[data-local]");
}

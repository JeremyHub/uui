// Running one turn: build the prompt, stream the reply, emit events as they become
// applicable. The same two shapes as before -- a first screen that streams body content
// straight into the parser, and every turn after it that rewrites only what changes.

import { PATCH_SYSTEM_PROMPT, SHELL_SYSTEM_PROMPT } from "./prompts.js";
import { PatchParser } from "./parser.js";
import { regionsOf, renderScreen } from "./screen.js";

const TRAILING_JUNK_RE = /(?:\s|`{3,}[a-zA-Z0-9]*|<\/body>|<\/html>)+$/i;
// Enough to hold back a closing fence, a </body></html>, and their whitespace.
const TAIL_HOLDBACK = 24;
const BODY_OPEN_RE = /<body\b[^>]*>/i;

export const SHELL_MAX_TOKENS = 2200;
export const PATCH_MAX_TOKENS = 1600;

/**
 * Where the model's actual body content begins, or -1 if it is not clear yet.
 *
 * The model is asked for body content and nothing else, and mostly complies -- but it
 * also opens with "Sure, here it is:" or wraps the lot in a full document. Since the
 * reply is streamed into the iframe's parser, junk has to be identified before it is
 * written rather than cleaned up afterwards.
 */
export function bodyContentStart(text) {
  const lower = text.toLowerCase();
  const section = lower.indexOf("<section");
  if (section !== -1) return section;
  const body = BODY_OPEN_RE.exec(text);
  if (body) return body.index + body[0].length;
  // No structural landmark yet. Once enough has arrived that none is coming, fall back
  // to the first tag of any kind rather than stalling the stream forever.
  if (text.length > 800) return text.includes("<") ? text.indexOf("<") : 0;
  return -1;
}

export function buildPatchMessage({ concept, doc, action, recent }) {
  const parts = [`APP CONCEPT:\n${concept || "(unspecified)"}`];
  if (recent?.length) {
    parts.push("RECENTLY:\n" + recent.slice(-3).map((r) => `- ${r}`).join("\n"));
  }
  parts.push("SCREEN:\n" + (renderScreen(doc) || "(nothing yet)"));
  parts.push(`USER ACTION:\n${JSON.stringify(action, null, 2)}`);
  return parts.join("\n\n");
}

/**
 * Stream the body as it is generated, so the page fills in rather than appearing.
 *
 * The document around it -- doctype, head, stylesheet -- belongs to the app and is
 * already on screen before this is called, so the model writes content and nothing else.
 */
export async function* runShell({ transport, concept, signal }) {
  yield { type: "phase", name: "building" };

  const parts = [];
  let pending = "";
  let started = false;

  for await (const piece of transport.chat({
    system: SHELL_SYSTEM_PROMPT,
    user: `APP CONCEPT:\n${concept || "a simple demo app"}`,
    maxTokens: SHELL_MAX_TOKENS,
    temperature: 0.7,
    signal,
  })) {
    parts.push(piece);
    if (!started) {
      const joined = parts.join("");
      const idx = bodyContentStart(joined);
      if (idx === -1) continue;
      started = true;
      pending = joined.slice(idx);
    } else {
      pending += piece;
    }
    // Hold back the tail: a model that wraps its answer in ```html would otherwise leave
    // the closing fence rendered as text at the bottom of the finished page.
    if (pending.length > TAIL_HOLDBACK) {
      yield { type: "screen_delta", text: pending.slice(0, -TAIL_HOLDBACK) };
      pending = pending.slice(-TAIL_HOLDBACK);
    }
  }

  if (!started) {
    // The model ignored the format entirely. Better to show what it said than to leave a
    // blank screen with nothing to explain it.
    pending = `<section data-region="main">${parts.join("")}</section>`;
  }
  yield { type: "screen_delta", text: pending.replace(TRAILING_JUNK_RE, "") };
  yield { type: "screen_end" };
}

/**
 * Stop a malformed #screen from wiping the app.
 *
 * A screen swap replaces all the body content, so getting it wrong is the one failure in
 * this design that destroys work rather than just looking wrong. The format asks for
 * <section data-region> blocks; a reply with none of them is a region's worth of content
 * that the model mislabelled, so treat it as one.
 */
export function guardScreen(event, { doc, action }) {
  if (event.type !== "screen" || event.html.includes("data-region")) return event;
  const regions = regionsOf(doc);
  if (!regions.length) return event;

  // Never land it on the region holding the control that was just clicked. The biggest
  // region on a page is often the navigation, and dropping an About page into the nav
  // deletes every way of getting anywhere -- worse than the blank screen this guards
  // against. Content the user asked for belongs somewhere other than the menu.
  const clickedIn = action?.elementData?.inRegion;
  const candidates = regions.filter((el) => el.getAttribute("data-region") !== clickedIn);
  const pool = candidates.length ? candidates : regions;
  const biggest = pool.reduce((a, b) => (b.innerHTML.length > a.innerHTML.length ? b : a));
  return {
    type: "region",
    id: biggest.getAttribute("data-region"),
    html: event.html,
    demoted: true,
  };
}

const RETRY_NUDGE =
  "\n\nYour last reply had a #plan but no #region block, so nothing changed on screen. " +
  "Reply again, and this time include the #region line and the full new HTML under it.";

async function* patchOnce({ transport, system, user, doc, action, signal }) {
  const parser = new PatchParser();
  for await (const piece of transport.chat({
    system, user, maxTokens: PATCH_MAX_TOKENS, temperature: 0.4, signal,
  })) {
    for (const event of parser.feed(piece)) yield guardScreen(event, { doc, action });
  }
  for (const event of parser.finish()) yield guardScreen(event, { doc, action });
}

export async function* runPatch({ transport, doc, concept, action, recent, signal }) {
  const user = buildPatchMessage({ concept, doc, action, recent });
  yield { type: "phase", name: "updating" };

  let produced = false;
  for await (const event of patchOnce({
    transport, system: PATCH_SYSTEM_PROMPT, user, doc, action, signal,
  })) {
    produced = produced || event.type === "region" || event.type === "screen";
    yield event;
  }

  if (!produced && !signal?.aborted) {
    // The model announced a plan and then wrote nothing under it, so the click did
    // nothing at all -- the worst outcome available, since the user cannot tell a broken
    // control from a slow one. Nothing has been applied yet, so there is nothing to undo,
    // and the failure is fast precisely because it generated almost no tokens.
    yield* patchOnce({
      transport, system: PATCH_SYSTEM_PROMPT, user: user + RETRY_NUDGE, doc, action, signal,
    });
  }
}

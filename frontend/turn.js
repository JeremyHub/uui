// Running one turn: build the prompt, stream the reply, emit events as they become
// applicable. The same two shapes as before -- a first screen that streams body content
// straight into the parser, and every turn after it that rewrites only what changes.
import { fetchForPrompt } from "./apis.js";
import { ADDRESS_SYSTEM_PROMPT, PATCH_SYSTEM_PROMPT, SHELL_SYSTEM_PROMPT } from "./prompts.js";
import { PatchParser } from "./parser.js";
import { regionsOf, renderScreen } from "./screen.js";
const TRAILING_JUNK_RE = /(?:\s|`{3,}[a-zA-Z0-9]*|<\/body>|<\/html>)+$/i;
// Enough to hold back a closing fence, a </body></html>, and their whitespace.
const TAIL_HOLDBACK = 24;
const BODY_OPEN_RE = /<body\b[^>]*>/i;
export const SHELL_MAX_TOKENS = 2200;
export const PATCH_MAX_TOKENS = 1600;
const FETCH_DIRECTIVE_RE = /^#fetch\s+(\S+)/;
// Long enough to have seen the first line of any real reply, short enough that a reply
// which is not a data request is barely delayed by the wait.
const DECIDE_AFTER = 200;
/** Carry on an iterator whose first value has already been taken. */
async function* resume(first, iterator) {
    if (first.done)
        return;
    yield first.value;
    while (true) {
        const next = await iterator.next();
        if (next.done)
            return;
        yield next.value;
    }
}
/**
 * Stream a reply, unless the model opens by asking for data.
 *
 * `#fetch <url>` on the first line means the model cannot answer without something it
 * does not have. Everything it would write after that line is guesswork, so the moment
 * the directive is recognised the generation is cut off -- which is also why asking for
 * data is cheap: the wasted call produces about ten tokens.
 *
 * The URL is reported through `request` rather than the stream, because the caller has
 * to act on it before any of the reply can be used.
 */
async function* replyStream({ engine, system, user, maxTokens, temperature, signal, request }) {
    const child = new AbortController();
    const relay = () => child.abort();
    if (signal?.aborted)
        return;
    signal?.addEventListener("abort", relay);
    let head = "";
    let decided = false;
    // Returns true if `head` turned out to be a data request. A whole reply can be shorter
    // than DECIDE_AFTER and carry no newline at all -- "#fetch <url>" is exactly that --
    // so this has to run when the stream ends as well as during it, or the directive gets
    // written into the page as text.
    const isDataRequest = () => {
        const nl = head.indexOf("\n");
        const first = (nl === -1 ? head : head.slice(0, nl)).trim();
        const asked = FETCH_DIRECTIVE_RE.exec(first);
        if (!asked)
            return false;
        request.url = asked[1].replace(/[)>,.]+$/, "");
        return true;
    };
    try {
        for await (const piece of engine.chat({
            system, user, maxTokens, temperature, signal: child.signal,
        })) {
            if (decided) {
                yield piece;
                continue;
            }
            head += piece;
            if (head.indexOf("\n") === -1 && head.length < DECIDE_AFTER)
                continue;
            if (isDataRequest()) {
                child.abort();
                return;
            }
            decided = true;
            yield head;
        }
    }
    catch (e) {
        if (e?.name !== "AbortError")
            throw e;
    }
    finally {
        signal?.removeEventListener("abort", relay);
    }
    if (!decided && head && !isDataRequest())
        yield head;
}
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
    if (section !== -1)
        return section;
    const body = BODY_OPEN_RE.exec(text);
    if (body)
        return body.index + body[0].length;
    // No structural landmark yet. Once enough has arrived that none is coming, fall back
    // to the first tag of any kind rather than stalling the stream forever.
    if (text.length > 800)
        return text.includes("<") ? text.indexOf("<") : 0;
    return -1;
}
export function buildPatchMessage({ concept, doc, action, memory, address }) {
    const parts = [`APP CONCEPT:\n${concept || "(unspecified)"}`];
    if (address)
        parts.push(`ADDRESS:\n${address}`);
    // What the session has established so far. The screen shows the present; this is the
    // only reason a later turn can still know what the user said five screens ago.
    if (memory)
        parts.push(memory);
    parts.push("SCREEN:\n" + (renderScreen(doc) || "(nothing yet)"));
    parts.push(`USER ACTION:\n${JSON.stringify(action, null, 2)}`);
    return parts.join("\n\n");
}
export const ADDRESS_MAX_TOKENS = 32;
const ADDRESS_RE = /(?:https?:\/\/)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\/[^\s"'<>`]*)?/i;
/**
 * Ask the model for the app's web address: one line, a few tokens, before the first screen.
 *
 * Returns "" when the reply holds nothing shaped like an address, and the caller makes
 * one up -- the bar belongs to the app, and is never left empty for a model's sake.
 */
export async function nameAddress({ engine, concept, signal }) {
    const child = new AbortController();
    const relay = () => child.abort();
    if (signal?.aborted)
        return "";
    signal?.addEventListener("abort", relay);
    let text = "";
    try {
        for await (const piece of engine.chat({
            system: ADDRESS_SYSTEM_PROMPT, user: `APP CONCEPT:\n${concept || "a simple demo app"}`,
            maxTokens: ADDRESS_MAX_TOKENS, temperature: 0.7, signal: child.signal,
        })) {
            text += piece;
            // One line is all it was asked for; anything after it is commentary.
            if (text.trim() && text.trimStart().includes("\n")) {
                child.abort();
                break;
            }
        }
    }
    catch (e) {
        if (e?.name !== "AbortError")
            throw e;
    }
    finally {
        signal?.removeEventListener("abort", relay);
    }
    const first = text.trim().split("\n")[0];
    const match = ADDRESS_RE.exec(first);
    return match ? match[0].replace(/[.,;:)]+$/, "") : "";
}
/**
 * Stream the body as it is generated, so the page fills in rather than appearing.
 *
 * The document around it -- doctype, head, stylesheet -- belongs to the app and is
 * already on screen before this is called, so the model writes content and nothing else.
 */
export async function* runShell({ engine, concept, address, signal, getData = fetchForPrompt }) {
    yield { type: "phase", name: "building" };
    let user = `APP CONCEPT:\n${concept || "a simple demo app"}`;
    if (address)
        user += `\n\nADDRESS:\n${address}`;
    const request = {};
    const ask = (u, req) => replyStream({
        engine, system: SHELL_SYSTEM_PROMPT, user: u,
        maxTokens: SHELL_MAX_TOKENS, temperature: 0.7, signal, request: req,
    })[Symbol.asyncIterator]();
    // Pull one value to learn whether the model wanted data first. Exactly one, because
    // the whole point of the first turn is that it paints while it is being written, and
    // buffering the reply to inspect it would throw that away. A data request is reported
    // before anything is yielded, so one value is all it takes to know.
    let iterator = ask(user, request);
    let first = await iterator.next();
    if (request.url) {
        // One round trip only: a second would be a model looping on data it cannot use.
        yield { type: "fetching", url: request.url };
        user += `\n\n${await getData(request.url)}`;
        iterator = ask(user, {});
        first = await iterator.next();
    }
    const source = resume(first, iterator);
    const parts = [];
    let pending = "";
    let started = false;
    for await (const piece of source) {
        parts.push(piece);
        if (!started) {
            const joined = parts.join("");
            const idx = bodyContentStart(joined);
            if (idx === -1)
                continue;
            started = true;
            pending = joined.slice(idx);
        }
        else {
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
    if (event.type !== "screen" || event.html.includes("data-region"))
        return event;
    const regions = regionsOf(doc);
    if (!regions.length)
        return event;
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
const RETRY_NUDGE = "\n\nYour last reply had a #plan but no #region block, so nothing changed on screen. " +
    "Reply again, and this time include the #region line and the full new HTML under it.";
async function* patchOnce({ engine, user, doc, action, signal, request = {} }) {
    const parser = new PatchParser();
    for await (const piece of replyStream({
        engine, system: PATCH_SYSTEM_PROMPT, user,
        maxTokens: PATCH_MAX_TOKENS, temperature: 0.4, signal, request,
    })) {
        for (const event of parser.feed(piece))
            yield guardScreen(event, { doc, action });
    }
    for (const event of parser.finish())
        yield guardScreen(event, { doc, action });
}
export async function* runPatch({ engine, doc, concept, action, memory, address, signal, getData = fetchForPrompt, }) {
    let user = buildPatchMessage({ concept, doc, action, memory, address });
    yield { type: "phase", name: "updating" };
    const produced = { any: false };
    const run = async function* (message, request) {
        for await (const event of patchOnce({
            engine, user: message, doc, action, signal, request,
        })) {
            produced.any = produced.any || ["region", "screen", "none"].includes(event.type);
            yield event;
        }
    };
    const request = {};
    yield* run(user, request);
    if (request.url) {
        // The model asked for data instead of answering, so nothing has been applied yet.
        // One round trip only -- a second would be a loop.
        yield { type: "fetching", url: request.url };
        user += `\n\n${await getData(request.url)}`;
        yield* run(user, {});
    }
    if (!produced.any && !signal?.aborted) {
        // The model announced a plan and then wrote nothing under it -- not even #none, which
        // is how it says nothing should change -- so the click did nothing at all. That is
        // the worst outcome available, since the user cannot tell a broken control from a
        // slow one. Nothing has been applied yet, so there is nothing to undo,
        // and the failure is fast precisely because it generated almost no tokens.
        yield* run(user + RETRY_NUDGE, {});
    }
}

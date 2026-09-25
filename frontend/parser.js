// Incremental parser for the #plan / #url / #region / #screen / #none / #end reply format.
//
// Emits events as soon as a marker line proves the previous block is finished, so a
// region reaches the screen while the model is still writing the next one. Within a
// region it goes further and emits each complete top-level element as it closes:
// waiting for the whole block means a region of any size is a blank wait for as long as
// it takes to write, while the model is producing renderable elements the entire time.
import { ELISION_RE } from "./screen.js";
const FENCE_LINE_RE = /^\s*```[a-zA-Z0-9]*\s*$/;
const BODY_WRAP_RE = /^<body\b[^>]*>|<\/body>$/gi;
const TAG_RE = /<(\/?)([a-zA-Z][\w-]*)/g;
const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr"]);
/**
 * Scrub compaction artifacts the model copied out of its own prompt.
 *
 * Small models treat the elided screen as a template and will happily echo
 * "<!-- and 5 more <li> ... -->" straight into the page. The marker exists only to save
 * prompt tokens, so it is stripped unconditionally on the way back out.
 */
export function cleanOutput(html) {
    return html.replace(ELISION_RE, "").trim();
}
/**
 * Cut `text` after each element that closes at the top level.
 *
 * Cuts land only where the tag depth returns to zero, so a chunk is always renderable on
 * its own -- never half an element. Depth is recomputed over the whole remainder each
 * time rather than carried between calls, because the remainder still contains the tags
 * that produced it and counting both double-counts. Models do not reliably put newlines
 * between elements, so this works on the text rather than line by line.
 */
export function splitCompleteElements(text) {
    const finished = [];
    let start = 0, depth = 0, match;
    TAG_RE.lastIndex = 0;
    while ((match = TAG_RE.exec(text)) !== null) {
        const tag = match[2].toLowerCase();
        if (VOID_TAGS.has(tag))
            continue;
        depth += match[1] ? -1 : 1;
        if (depth > 0)
            continue;
        const close = text.indexOf(">", TAG_RE.lastIndex);
        // The tag that would close this element is still being written -- "</div" with no
        // ">" yet. Cutting here emits a broken half-tag into the page, so wait instead: the
        // rest of it is in the next piece.
        if (close === -1)
            break;
        finished.push(text.slice(start, close + 1));
        start = close + 1;
        depth = 0;
        TAG_RE.lastIndex = start;
    }
    return { finished, remainder: text.slice(start) };
}
export class PatchParser {
    buf = "";
    kind = null;
    regionId = null;
    body = [];
    unflushed = ""; // region text not yet complete enough to send
    *feed(text) {
        this.buf += text;
        let nl;
        while ((nl = this.buf.indexOf("\n")) !== -1) {
            const line = this.buf.slice(0, nl);
            this.buf = this.buf.slice(nl + 1);
            yield* this.#line(line);
        }
        // Inside a region, do not wait for a newline. Models routinely write a whole region
        // on one line, and holding the buffer until it ends hands the block over in a single
        // piece -- exactly the blank wait chunking exists to remove.
        //
        // Only take a fragment that is unambiguously markup, though: a partial "#end" or a
        // half-written code fence read as content would be rendered into the page, because
        // the line-level checks that strip them have not seen a whole line yet.
        const fragment = this.buf.trimStart();
        if (this.kind === "region" && fragment
            && !fragment.startsWith("#") && !fragment.startsWith("`")
            && (this.unflushed || fragment.startsWith("<"))) {
            yield* this.#consumeBody(this.buf);
            this.buf = "";
        }
    }
    *finish() {
        if (this.buf) {
            yield* this.#line(this.buf);
            this.buf = "";
        }
        const event = this.#flush();
        if (event)
            yield event;
    }
    #flush() {
        if (this.kind === null)
            return null;
        this.unflushed = "";
        const html = cleanOutput(this.body.join(""));
        let event = null;
        if (html) {
            if (this.kind === "screen") {
                // Models often wrap a #screen block in <body> despite being asked for its
                // contents; that tag would land inside the real body if kept.
                event = { type: "screen", html: html.replace(BODY_WRAP_RE, "").trim() };
            }
            else {
                event = { type: "region", id: this.regionId, html };
            }
        }
        this.kind = null;
        this.regionId = null;
        this.body = [];
        return event;
    }
    *#line(line) {
        const stripped = line.trim();
        if (FENCE_LINE_RE.test(line))
            return;
        if (stripped.startsWith("#plan")) {
            const event = this.#flush();
            if (event)
                yield event;
            yield { type: "plan", text: stripped.slice(5).trim() };
        }
        else if (stripped.startsWith("#url")) {
            const event = this.#flush();
            if (event)
                yield event;
            const url = stripped.slice(4).trim();
            if (url)
                yield { type: "url", url };
        }
        else if (stripped.startsWith("#region")) {
            const event = this.#flush();
            if (event)
                yield event;
            this.kind = "region";
            const id = stripped.slice(7).trim().replace(/^["']|["']$/g, "") || "main";
            this.regionId = id;
            this.body = [];
            this.unflushed = "";
            yield { type: "region_open", id };
        }
        else if (stripped.startsWith("#screen")) {
            const event = this.#flush();
            if (event)
                yield event;
            this.kind = "screen";
            this.body = [];
        }
        else if (stripped.startsWith("#none")) {
            const event = this.#flush();
            if (event)
                yield event;
            yield { type: "none" };
        }
        else if (stripped.startsWith("#end")) {
            const event = this.#flush();
            if (event)
                yield event;
        }
        else if (this.kind === "region") {
            yield* this.#consumeBody(line + "\n");
        }
        else if (this.kind !== null) {
            this.body.push(line + "\n");
        }
    }
    *#consumeBody(text) {
        this.body.push(text);
        const { finished, remainder } = splitCompleteElements(this.unflushed + text);
        this.unflushed = remainder;
        for (const piece of finished) {
            const chunk = cleanOutput(piece);
            if (chunk)
                yield { type: "region_chunk", id: this.regionId, html: chunk };
        }
    }
}

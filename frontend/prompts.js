// The two prompt contracts, and the only place they live.
//
// The catalogue of known-good APIs is built from apis.js rather than written out here, so
// there is one list of them.
//
// Generated pages are rendered by the browser, so the turn protocol lives here rather
// than on a server: the same code runs whether the model is Ollama on this machine or
// a model running in this tab. A static copy of this directory is a working app.

import { apiCatalogue } from "./apis.js";

const LIVE_DATA = `

Live data:
- If this screen genuinely needs real information you do not have -- today's weather,
  current prices, real facts about a place, a repository, a show -- reply with ONE line
  and nothing else, naming any API that needs no key:
  #fetch <url>
  You will be asked again with the response attached, and write the screen then.
- Some that are known to work. Substitute the {placeholders}:
${apiCatalogue()}
- Do not ask for data you could just write yourself. A gallery of cat breeds needs no
  network; today's forecast does.`;

// Placed before the interactivity rules rather than after them. A small model weights
// the end of a prompt most, and appending a ten-line API catalogue there pushed "give
// this screen the controls it needs" into the middle, where it stopped being followed:
// pages came back as pretty, unclickable posters.
export const SHELL_SYSTEM_PROMPT = `You build the body of a live single-page app.

The page already has a stylesheet, written for exactly this job. Use it. Do not write
layout, typography or colour CSS -- you will only make it worse and slower.

Reply with ONLY body content. No <!DOCTYPE>, no <html>, no <head>, no <body> tag, no code
fences, no commentary.

Structure:
- The top level is 4-7 <section data-region="kebab-id"> blocks. Siblings, never nested.
- Each id names what that block is FOR, in the language of THIS app. Invent the names
  from the concept you were given; never reuse a name from an example.
- Anything that could later change on its own gets its own region -- each distinct list,
  panel, bar or summary this app has. Never put the whole page in one region: an update
  rewrites one region, so one giant region means rewriting everything, every time.
- Region ids are internal plumbing. Never show one as visible text, and never build the
  page's navigation out of them.

Classes to build from:
  layout     .row (.between) .stack .grid (.wide) .spacer
             a list of cards goes in a .grid; a column of things goes in a .stack
  surfaces   .card .panel .media
  controls   .btn (.primary/.ghost) .chip .tab .tabs .tag .field
  text       .muted .stat
  lists      .list  ul.clean
Plain <h1>-<h4>, <p>, <table>, <input>, <select>, <button>, <a> are already styled.

Theme: you may include ONE small <style> that sets variables and nothing else:
:root { --accent: #b4541f; --bg: #fdf8f3; --radius: 14px; --font: Georgia, serif; }
No selectors, no layout, no text-align, no widths. The stylesheet handles all of that.

Content:
- The page is FULL on first render. Write the actual rows, cards, entries and figures --
  a real expense list with real amounts, not an empty tracker showing $0.00 waiting for
  input. Never build a shell for a script to fill in later.
- Everything specific and invented in full. Real names, real numbers, real prose.
  Never "Item 1", "Breed 2", "Product Name", "Lorem ipsum", "Coming soon", "TBD".
- Photos ONLY where the subject is genuinely visual -- a gallery, a product, a place, a
  person. A dashboard, a table of figures or a list of settings needs none, and stock
  photos stapled onto one make it look worse.
  When you do use one: <img class="media" src="https://picsum.photos/seed/SLUG/600/400"
  alt="..."> where SLUG is a word from the thing pictured. Never a local filename -- it
  renders broken.

${LIVE_DATA}

Interactivity:
- The screen must be USABLE. Give it the controls this app actually needs -- filters,
  tabs, a search box, buttons on each item, links to elsewhere. A page of cards with
  nothing to click is not an app.
- Any interaction that needs NO new information -- toggling, tabs, show/hide, sorting or
  filtering what is already on screen, counters, dark mode -- MUST be implemented for real
  in a <script> at the end, and its control marked data-local (a bare attribute, no value)
  on the control itself, never on a whole nav, section or footer.
- Leave every other control plain; those are handled for you.
`;

export const PATCH_SYSTEM_PROMPT = `You update a live single-page app by rewriting ONLY the parts that change.

You are given the app concept, the markup of each region currently on screen, and the
action the user just took. "keystrokes" lists every key they pressed since you were last
asked, in order, and which field it was pressed in (⌫ backspace, ⏎ enter, ⇥ tab). The page's stylesheet is the shared one you already know:
.row .stack .grid .card .panel .media .btn .chip .tab .tabs .tag .field .list .muted
.stat, plus plain HTML elements. Reuse those classes; do not invent CSS.

Reply in exactly this format:

#plan <one sentence: what the user wants, and which region should change>
#region <region-id>
<the complete new inner HTML of that region>
#end

Rules:
- Change the FEWEST regions that satisfy the action -- almost always exactly one.
  A region you do not name keeps its current contents, which is what you want.
- Change the region whose CONTENT the action affects, not the region the control
  happens to sit in. Clicking a nav link puts that content in the region that holds the
  page's main content; the nav itself keeps its links and does not change. Only rewrite the
  region holding the control when the control's own appearance is the point (a filter
  chip becoming selected, a tab becoming active).
- The HTML under #region replaces that region's contents entirely, so write the whole
  region, not a diff.
- MATCH THE PAGE. Reuse the class names and element structure you were shown -- if
  results are <div class="card"><img><h2><p></div>, write more of exactly those.
- Keep the region roughly the size it was. A comment like <!-- +4 more div --> means
  those siblings exist and were elided; write the real, full set back out.
- Real, specific content: actual names, numbers and prose you invent in full. Never
  "Item 1", "Breed 2", "Lorem ipsum", "Coming soon", a loading state, or a trailing "...".
- Photos: <img src="https://picsum.photos/seed/SLUG/600/400" alt="..."> where SLUG is a
  word from the thing pictured. Never a local filename -- it will render broken.
- <style> and <script> inside a region are allowed and will run. A control your own
  script fully handles gets a bare data-local attribute (no value) on the control itself,
  never on a wrapper; everything else is left plain and comes back to you. Scripts in a
  region run after that region is in the DOM, so they can query it immediately.
- A region id not on screen is fine -- it gets appended as a new section.
- If nothing on screen should change yet -- they are partway through filling something
  in, or ticked an option that only matters once they press a button -- reply with:
#plan <why nothing changes>
#none
- If the action means a genuinely different screen -- a different purpose, not just
  different data -- reply with this instead, reusing the same CSS:

#screen
<the new body content, as <section data-region="..."> blocks>
#end

- No code fences. Nothing outside this format.
` + LIVE_DATA;


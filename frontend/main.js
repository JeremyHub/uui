// Wiring: pick a model, run turns, apply what comes back, and guess at what is next.
//
// The protocol lives in turn.js and the page handling in dom.js; this is the part that
// knows there is a user watching. Nothing here is specific to where the model runs.

import {
  FIELDS, KeyLog, LOCAL_GRACE_MS, allControls, appendToRegion, applyRegion, applyScreen,
  changeIsRequest, collectFormState, describeElement, ensureRegions, errorRegion, findControl, isClickable, isLocal,
  keySymbol, openRegion, repairImages, runScripts, splitOversizedRegions, submitsOnEnter,
} from "./dom.js";
import { Journal } from "./journal.js";
import { SHELL_MAX_TOKENS, nameAddress, runPatch, runShell } from "./turn.js";
import { createEngine, pickModel, serverHost, tabHost, webGPUCapability } from "./engine.js";

const appEl = document.getElementById("app");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");
const speculateEl = document.getElementById("speculate");
const hostEl = document.getElementById("engine");
const modelEl = document.getElementById("model");

const delegated = new WeakSet();   // documents whose click/submit delegation is live

// The iframe starts on about:blank, and Chrome switches the Navigation API off for that
// first document, so nothing a generated page does to navigate could be intercepted.
// A blank page of the app's own, loaded first, is an ordinary document -- and stays one
// after document.open() -- so the frame starts from that instead.
const frameReady = new Promise((resolve) => {
  appEl.addEventListener("load", function loaded() {
    if (appEl.contentWindow.location.protocol !== "blob:") return;
    appEl.removeEventListener("load", loaded);
    resolve();
  });
  appEl.src = URL.createObjectURL(new Blob(["<!DOCTYPE html><title></title>"], { type: "text/html" }));
});
let concept = "";
const journal = new Journal();     // what the session has established, compacted as it grows
const log = [];                    // debug transcript
let busy = false;
const keyLog = new KeyLog();       // keys pressed in the page since the model last saw them
// The update in flight, while there is one: { abort, rendering, done }. Until the model
// starts writing, nothing on screen has changed, so a newer request can replace it.
let turn = null;
let actionSeq = 0;
let engine = null;

function renderTranscript() {
  transcriptEl.textContent = JSON.stringify({
    concept, engine: engine?.label,
    memory: journal.summary, recent: journal.entries.map((e) => journal.line(e)),
    log: log.slice(-12),
  }, null, 2);
}

// --- the loading overlay ----------------------------------------------------
//
// While a turn is running the page underneath is mid-edit: half a region swapped in,
// controls that are about to be replaced. Clicking it did nothing (the turn in flight
// wins) but looked like it should, which is worse than being told to wait. The overlay
// covers the generated page only -- the top bar stays live, so a run is still escapable.

const loadingEl = document.getElementById("loading");
const loadingTitle = document.getElementById("loading-title");
const loadingDetail = document.getElementById("loading-detail");
const loadingBar = document.getElementById("loading-bar");

let loadingTimer = null;

const loading = {
  // `delay` keeps the overlay off screen for turns that finish almost immediately --
  // a guessed click applies in milliseconds, and flashing a progress card over it would
  // make the fastest thing the app does look like the slowest.
  show(title, { detail = "", subtle = false, delay = 0 } = {}) {
    clearTimeout(loadingTimer);
    const reveal = () => {
      loadingTitle.textContent = title;
      loadingDetail.textContent = detail;
      loadingEl.classList.toggle("subtle", subtle);
      loadingEl.hidden = false;
    };
    if (delay) loadingTimer = setTimeout(reveal, delay);
    else reveal();
    this.indeterminate();
  },
  detail(text) { loadingDetail.textContent = text; },
  indeterminate() { loadingBar.classList.add("indeterminate"); loadingBar.firstElementChild.style.width = ""; },
  progress(fraction) {
    loadingBar.classList.remove("indeterminate");
    loadingBar.firstElementChild.style.width = `${Math.min(100, Math.max(0, fraction * 100))}%`;
  },
  hide() {
    clearTimeout(loadingTimer);
    loadingEl.hidden = true;
  },
};

// --- status -----------------------------------------------------------------

let timerId = null, timerStart = 0, timerLabel = "";

function setStatus(text, extra = "") {
  statusEl.innerHTML = "";
  if (text) statusEl.append(text);
  if (extra) {
    statusEl.append(" · ");
    const span = document.createElement("span");
    span.className = "hit";
    span.textContent = extra;
    statusEl.append(span);
  }
}

function startTimer(label) {
  timerLabel = label;
  timerStart = performance.now();
  clearInterval(timerId);
  timerId = setInterval(() => {
    setStatus(`${timerLabel} ${((performance.now() - timerStart) / 1000).toFixed(1)}s`);
  }, 100);
  setStatus(`${label} 0.0s`);
}

function stopTimer(suffix = "") {
  clearInterval(timerId);
  timerId = null;
  const secs = ((performance.now() - timerStart) / 1000).toFixed(1);
  setStatus(`${secs}s`, suffix);
  return Number(secs);
}

// --- choosing where the model runs ------------------------------------------

let baseCss = "";
const baseCssReady = fetch("base.css").then((r) => r.text()).then((css) => { baseCss = css; });

function documentHead(title) {
  return '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    `<title>${title.replace(/[<&]/g, "")}</title>` +
    `<style data-uui-base>${baseCss}</style></head><body>`;
}

function option(value, label, extra = {}) {
  const el = document.createElement("option");
  el.value = value;
  el.textContent = label;
  Object.assign(el, extra);
  return el;
}

// Where the model can run. The engine is the same code on every host; a host only says
// what models it has and how to start one -- see engine.js.
const hosts = new Map();

function currentHost() {
  return hosts.get(hostEl.value);
}

// --- when the tab cannot use the graphics card ------------------------------------
//
// Served statically, the model runs in the tab over WebGPU, and whether the tab gets the
// graphics card is a browser setting. Chrome on Linux ships with WebGPU off; turning it
// on without Vulkan gets SwiftShader, which runs on the CPU. Both used to show up as a
// dropdown saying "no models available" or "no GPU", which reads as the app being broken
// or the machine lacking a card, when the fix is two flags and a relaunch. So say that,
// where it is seen, and in terms of what to turn on.

function gpuNotice(gpu) {
  if (gpu.ok && !gpu.software) return null;
  const ua = navigator.userAgent;
  const chromium = /Chrome\//.test(ua);
  const scheme = /Edg\//.test(ua) ? "edge" : "chrome";
  const browser = scheme === "edge" ? "Edge" : "Chrome";
  const flag = (name) => ({ code: `${scheme}://flags/#${name}` });

  let fix;
  if (chromium && /Linux/.test(ua) && !/Android|CrOS/.test(ua)) {
    fix = gpu.ok
      ? ["Turn on ", flag("enable-vulkan"), ` too, then relaunch ${browser}.`]
      : ["Turn on ", flag("enable-unsafe-webgpu"), " and ", flag("enable-vulkan"),
        `, then relaunch ${browser}.`];
  } else if (chromium) {
    fix = [`Check that "Use graphics acceleration when available" is on in ${browser}'s `,
      "system settings, and that ", { code: `${scheme}://gpu` },
      " lists WebGPU as hardware accelerated."];
  } else {
    fix = ["A current Chrome or Edge is the most reliable way to get it."];
  }

  return gpu.ok
    ? { title: "Running on the CPU, not your graphics card",
      body: ["Your browser offered WebGPU without the card behind it, so models will be ",
        "very slow. ", ...fix] }
    : { title: "Your browser isn't giving this page WebGPU",
      body: ["The model runs on your graphics card through WebGPU, so nothing can run until ",
        "it's on. ", ...fix, " Or run the uui server, where Ollama runs the model."] };
}

function showNotice(notice) {
  const el = document.getElementById("notice");
  el.hidden = !notice;
  if (!notice) return;
  const title = document.createElement("strong");
  title.textContent = notice.title;
  const body = document.createElement("span");
  for (const part of notice.body) {
    if (typeof part === "string") { body.append(part); continue; }
    // A page cannot link to chrome:// URLs, so make them easy to copy instead.
    const code = document.createElement("code");
    code.textContent = part.code;
    body.append(code);
  }
  el.replaceChildren(title, body);
}

// Auto is the default because the honest answer for most people is "whichever one
// works", and the wrong choice here is a multi-gigabyte download that ends in an
// out-of-memory error.
//
// Listing the in-tab models means importing WebLLM from a CDN, which takes seconds. A
// start in that window used to fail with "no in-tab model is available on this device",
// which is false and reads as final, so a start waits on whichever listing is current.
let modelsReady = Promise.resolve();

// Set when neither host can run anything: the reason, for the model picker and a start
// that has nothing to start.
let unavailable = "";

function populateModels(known = null) {
  modelsReady = fillModels(known);
  return modelsReady;
}

async function fillModels(known) {
  const host = currentHost();
  modelEl.replaceChildren(option("", "loading…"));
  modelEl.disabled = true;

  let models = [];
  try {
    models = known ?? await host.listModels();
  } catch (e) {
    console.warn("could not list models", e);
  }
  if (!models.length) {
    modelEl.replaceChildren(option("", unavailable ? "WebGPU unavailable" : "no models available"));
    modelEl.title = unavailable || "Which model";
    modelEl.dataset.auto = "";
    modelEl.disabled = false;
    return;
  }
  const auto = pickModel(models, host.budgetMB);
  modelEl.replaceChildren(
    option("auto", auto ? `Auto · ${auto.id}` : "Auto"),
    ...models.map((m) => {
      const el = option(
        m.id,
        m.sizeMB ? `${m.id} (${(m.sizeMB / 1024).toFixed(1)} GB)` : m.id,
        { disabled: host.strictBudget && m.sizeMB !== null && m.sizeMB > host.budgetMB * 1.3 },
      );
      if (m.metered) el.dataset.metered = "";
      return el;
    }),
  );
  modelEl.value = "auto";
  modelEl.dataset.auto = auto?.id ?? "";
  modelEl.disabled = false;
}

function buildEngine() {
  const modelId = modelEl.value === "auto" ? modelEl.dataset.auto : modelEl.value;
  if (!modelId && unavailable) throw new Error(unavailable);
  if (!modelId) throw new Error(`no model is available ${currentHost().label.toLowerCase()}`);
  return createEngine(currentHost(), modelId);
}

async function ensureEngineReady() {
  if (!engine) engine = buildEngine();
  if (engine.ready) return;

  // Delayed so a load that is over in moments -- the server's, or a cached one already
  // under way -- does not flash a card up for nothing.
  loading.show("Loading the model", { detail: currentHost().loadingDetail, delay: 150 });
  await engine.prepare(({ fraction, text }) => {
    if (fraction > 0) loading.progress(fraction);
    if (text) loading.detail(text);
  });
}

async function setupEngineChoice() {
  const gpu = await webGPUCapability();
  // Whether this copy of the app has a server behind it. Served as plain static files --
  // from a file:// path, a CDN, someone's GitHub Pages -- there is nothing to ask, and
  // the tab is the only place a model can run.
  const server = serverHost({
    // The server usually runs on the card this tab can see, so its size steers Auto
    // there too. A software adapter says nothing about the real card.
    budgetMB: gpu.ok && !gpu.software ? gpu.budgetMB : Infinity,
  });
  const serverModels = await server.listModels().catch(() => null);

  if (serverModels) hosts.set(server.id, server);
  if (gpu.ok) {
    const tab = tabHost(gpu);
    hosts.set(tab.id, tab);
  }
  // Nothing can run here. Offer the server anyway, so the failure says what to start.
  if (!hosts.size) {
    hosts.set(server.id, server);
    unavailable = gpu.why;
  }
  // With a server there is a fast option whatever the tab can do. Without one, the tab is
  // all there is, and a browser setting is the difference between working and not.
  if (!serverModels) showNotice(gpuNotice(gpu));

  hostEl.replaceChildren(...[...hosts.values()].map((h) => option(h.id, h.label)));
  hostEl.title = gpu.ok ? "Where the model runs" : `Where the model runs. ${gpu.why}`;
  hostEl.value = hosts.keys().next().value;
  hostEl.addEventListener("change", () => { dropEngine(); populateModels().then(warmUp); });
  modelEl.addEventListener("change", () => {
    // Guessing ahead spends several calls per click. Free on a local model, but on a
    // metered one that is someone's usage limit, so it waits to be switched back on.
    if (modelEl.selectedOptions[0]?.dataset.metered !== undefined) speculateEl.checked = false;
    dropEngine();
    warmUp();
  });
  await populateModels(hostEl.value === server.id ? serverModels ?? [] : null);
  warmUp();
}

// A model that loaded once will load again without the network, in seconds rather than
// minutes. Waiting for the button to start that means the GPU sits idle while someone
// types and then they wait anyway, so start as soon as the choice is made. Nothing
// uncached is fetched unasked: that is a multi-gigabyte download.
async function warmUp() {
  if (engine || busy || !currentHost()?.warmUp) return;
  try {
    const candidate = buildEngine();
    if (engine || !(await candidate.isCached())) return;
    engine = candidate;
    await candidate.prepare();
  } catch (e) {
    console.warn("could not load the model ahead of time", e);
  }
}

// The one on the card has to go before another can fit.
function dropEngine() {
  engine?.dispose?.();
  engine = null;
}

// --- guessing ahead ---------------------------------------------------------

const predictions = new Map();    // signature -> events, once a guess is complete
// Long enough that sweeping the pointer across a toolbar does not fire on every control,
// short enough to still get a head start on the click.
const HOVER_DELAY_MS = 90;
let specAbort = null;
let specRun = 0;
let hoverTimer = null;

function signature(action) {
  const e = action.elementData;
  return JSON.stringify([action.event, e.tag, e.text, e.id, e.name, e.href, action.formValues]);
}

function cancelSpeculation() {
  clearTimeout(hoverTimer);
  specRun++;
  if (specAbort) { specAbort.abort(); specAbort = null; }
}

function speculationTargets(doc) {
  const seen = new Set();
  return allControls(doc)
    .filter((el) => isClickable(el, doc))
    .filter((el) => !isLocal(el))
    .filter((el) => {
      const text = el.textContent.trim();
      if (seen.has(text)) return false;
      seen.add(text);
      return true;
    })
    .slice(0, 3);
}

async function generatePrediction(doc, el, myRun) {
  const action = { event: "click", elementData: describeElement(el), formValues: collectFormState(doc) };
  const key = signature(action);
  if (predictions.has(key)) return true;

  specAbort = new AbortController();
  const events = [];
  try {
    for await (const event of runPatch({
      engine, doc, concept, action, memory: journal.toPrompt(), address, signal: specAbort.signal,
    })) {
      if (myRun !== specRun) return false;
      events.push(event);
    }
  } catch (e) {
    if (e.name !== "AbortError") console.warn("speculation failed", e);
    return false;
  } finally {
    specAbort = null;
  }
  if (myRun !== specRun || !events.some((e) => e.type === "region" || e.type === "screen")) return false;
  predictions.set(key, events);
  setStatus("", `ready: ${predictions.size} predicted`);
  return true;
}

// Idle-time work, in priority order: keep the session's memory from growing without
// bound, then guess at the next click. Folding the journal is one short call and it
// shrinks every prompt after it, so it earns its place ahead of the guessing.
async function speculate(doc) {
  if (!engine) return;
  const myRun = ++specRun;
  if (journal.needsCompaction()) {
    await journal.compact(engine);
    renderTranscript();
    if (myRun !== specRun) return;
  }
  if (!speculateEl.checked) return;
  for (const el of speculationTargets(doc)) {
    if (myRun !== specRun) return;
    await generatePrediction(doc, el, myRun);
  }
}

// Pointing at something is a much better guess than guessing in order, and it arrives a
// few hundred milliseconds before the click does. A hovered control jumps the queue.
//
// Driven by pointer movement rather than "mouseover", which also fires when the DOM
// changes underneath a cursor that has not moved at all. Since every turn replaces a
// region, that spurious hover cancelled the idle pass moments after it started, and the
// page ended up with one guess instead of three.
function predictOnHover(doc, el) {
  if (!speculateEl.checked || busy || isLocal(el) || !engine) return;
  const action = { event: "click", elementData: describeElement(el), formValues: collectFormState(doc) };
  // Already guessed: jumping the queue would cancel the idle pass to redo finished work.
  if (predictions.has(signature(action))) return;

  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    if (busy) return;
    cancelSpeculation();
    generatePrediction(doc, el, specRun);
  }, HOVER_DELAY_MS);
}

// --- turns ------------------------------------------------------------------

function applyEvent(doc, event, touched) {
  if (event.type === "url") setAddress(event.url);
  else if (event.type === "region_open") openRegion(doc, event.id);
  else if (event.type === "region_chunk") appendToRegion(doc, event.id, event.html);
  else if (event.type === "region") { applyRegion(doc, event.id, event.html); touched?.push(event.id); }
  else if (event.type === "screen") { applyScreen(doc, event.html); touched?.push("(whole screen)"); }
  else if (event.type === "error") applyRegion(doc, "uui-error", errorRegion(event.message));
  // "fetching" carries no content -- it says the model asked for real data and the app
  // is going to get it, which is worth saying out loud because it costs a round trip.
}

function applyEvents(doc, events) {
  let plan = "";
  for (const event of events) {
    if (event.type === "plan") plan = event.text;
    else applyEvent(doc, event);
  }
  return plan;
}

async function sendAction(action) {
  // The first screen is still being written; there is nothing to act on yet.
  if (busy && !turn) return;
  const mine = ++actionSeq;
  if (turn) {
    // Once the model is writing, the page is mid-rewrite under the overlay, and a request
    // then is dropped like a click on the overlay. Before that it has only been deciding,
    // so the newer request -- which carries every key since, too -- simply replaces it.
    if (turn.rendering) return;
    turn.abort.abort();
    await turn.done;
    if (mine !== actionSeq || busy) return;
  }
  busy = true;
  cancelSpeculation();
  const doc = appEl.contentDocument;
  const label = `${action.event} "${(action.elementData.text || action.elementData.label || "").slice(0, 40)}"`;
  const key = signature(action);
  const keystrokes = keyLog.take();
  if (keystrokes.length) action = { ...action, keystrokes };

  if (predictions.has(key)) {
    // Already generated while the user was deciding: no model call at all.
    timerStart = performance.now();
    const plan = applyEvents(doc, predictions.get(key));
    keyLog.consumed(keystrokes.length);
    journal.add({ label, plan, inputs: action.formValues });
    predictions.clear();
    stopTimer("instant (predicted)");
    log.push({ action: label, plan, predicted: true });
    renderTranscript();
    busy = false;
    speculate(doc);
    return;
  }
  predictions.clear();

  const current = { abort: new AbortController(), rendering: false };
  let finished;
  current.done = new Promise((resolve) => { finished = resolve; });
  turn = current;

  // No overlay while the model decides. It may decide nothing should change -- Enter in
  // a half-filled form, a box ticked ahead of pressing Search -- and covering the page
  // for that reads as the app stalling on every key. The status line shows it thinking;
  // the overlay comes up once there is new content on its way.
  const rendering = () => {
    if (current.rendering) return;
    current.rendering = true;
    loading.show("Updating", { detail: plan, subtle: true, delay: 180 });
  };

  startTimer("thinking");
  const touched = [];
  let plan = "", written = 0;
  try {
    // Normally already loaded. Not after the GPU has been lost, which takes the model
    // with it -- see engine.js.
    await ensureEngineReady();
    for await (const event of runPatch({
      engine, doc, concept, action, memory: journal.toPrompt(), address, signal: current.abort.signal,
    })) {
      if (event.type === "plan") { plan = event.text; timerLabel = plan.slice(0, 80); loading.detail(plan); }
      else if (event.type === "phase" || event.type === "none") continue;
      else if (event.type === "fetching") {
        rendering();
        timerLabel = "fetching live data";
        loading.detail(`Fetching ${new URL(event.url).host}…`);
        loading.indeterminate();
      } else {
        rendering();
        applyEvent(doc, event, touched);
      }
      written += (event.html ?? "").length;
      // No token count to divide by, so progress is measured against the cap the model
      // was given -- roughly right, and always moving forwards.
      if (written) loading.progress(Math.min(0.95, written / 1400));
    }
  } catch (e) {
    if (e.name !== "AbortError") applyRegion(doc, "uui-error", errorRegion(e.message));
  } finally {
    loading.hide();
  }

  turn = null;
  busy = false;
  if (current.abort.signal.aborted) {
    // Replaced by a newer request, which is waiting on this and takes these keys again.
    keyLog.returned();
    finished();
    return;
  }
  keyLog.consumed(keystrokes.length);
  const secs = stopTimer(touched.length ? ` · changed ${touched.join(", ")}` : " · no change");
  journal.add({ label, plan, inputs: action.formValues });
  log.push({ action: label, plan, touched, secs });
  renderTranscript();
  finished();
  speculate(doc);
}

// The first turn is the only one that writes a whole screen. It is streamed into the
// iframe's parser rather than assigned at the end, so the page fills in as it is written.
async function startFromConcept() {
  if (busy) return;
  concept = document.getElementById("concept").value.trim() || "something interesting";
  enterSession();
  busy = true;

  loading.show("Getting ready");
  try {
    await engineChosen;
    await modelsReady;
    await frameReady;
    await ensureEngineReady();
  } catch (e) {
    loading.hide();
    setStatus(`could not start: ${e.message}`);
    leaveSession();
    busy = false;
    return;
  }

  startTimer("making");
  loading.show("Making it for you", { detail: concept });
  await baseCssReady;

  // The address first, so the first screen is the page it names. The bar is the app's,
  // so a reply with no address in it gets one made up rather than an empty bar.
  try {
    setAddress(await nameAddress({ engine, concept }));
  } catch (e) {
    console.warn("could not name the address", e);
  }
  if (!address) setAddress(`https://${slugOf(concept)}.app/`);

  const doc = appEl.contentDocument;
  // document.open() reuses the same document object but strips every listener on it, so
  // the "already delegated" guard has to be cleared or delegation silently never
  // re-attaches -- and every click the generated page does not handle itself dies.
  doc.open();
  delegated.delete(doc);
  attachDelegation(doc);

  // Streaming into the parser means inline scripts run mid-parse, so a generated script
  // that reaches for an element written later throws and the page loses its local
  // interactivity. Watch for that and run the scripts again once the document is whole.
  let scriptFailed = false;
  appEl.contentWindow.addEventListener("error", () => { scriptFailed = true; }, true);

  doc.write(documentHead(concept));
  let written = 0;
  try {
    for await (const event of runShell({ engine, concept, address })) {
      if (event.type === "fetching") {
        loading.detail(`Fetching ${new URL(event.url).host}…`);
        loading.indeterminate();
      } else if (event.type === "screen_delta") {
        doc.write(event.text);
        written += event.text.length;
        loading.progress(Math.min(0.95, written / (SHELL_MAX_TOKENS * 2)));
      }
    }
  } catch (e) {
    doc.write(errorRegion(e.message));
  }
  if (!written) doc.write(errorRegion("Nothing came back. Try asking another way."));
  doc.write("</body></html>");
  doc.close();

  try {
    if (scriptFailed) runScripts(doc.body, doc);
    ensureRegions(doc);
    splitOversizedRegions(doc);
    repairImages(doc.body, doc);
  } catch (e) {
    console.error("post-shell fixups failed", e);
  }
  loading.hide();
  const secs = stopTimer();
  log.push({ action: "start", concept, secs });
  renderTranscript();
  busy = false;
  speculate(doc);
}

// --- input capture ----------------------------------------------------------

function attachDelegation(doc) {
  if (delegated.has(doc)) return;
  delegated.add(doc);

  doc.addEventListener("click", (e) => {
    const el = findControl(e.target, doc);
    if (!el) return;
    if (isLocal(el)) {
      // It submits its form, and the submit is handled below with the same grace. Both
      // becoming requests would have the second replace the first mid-decision.
      if (el.type === "submit" && el.form) return;
      // Let the page's own handler run, but never let a real href navigate away.
      if (el.tagName === "A") e.preventDefault();
      const before = doc.body.innerHTML;
      setTimeout(() => {
        if (doc.body.innerHTML !== before) return;
        sendAction({ event: "click", elementData: describeElement(el), formValues: collectFormState(doc) });
      }, LOCAL_GRACE_MS);
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    sendAction({ event: "click", elementData: describeElement(el), formValues: collectFormState(doc) });
  }, true);

  doc.addEventListener("submit", (e) => {
    const form = e.target.closest("form");
    if (!form) return;
    e.preventDefault();
    const send = () => sendAction({
      event: "submit",
      elementData: describeElement(e.submitter || form),
      formValues: collectFormState(doc),
    });
    // Same grace as a click: a form the page claims to handle but does not would
    // otherwise be a search box that silently does nothing, forever.
    if (isLocal(form) || isLocal(e.submitter || form)) {
      const before = doc.body.innerHTML;
      setTimeout(() => { if (doc.body.innerHTML === before) send(); }, LOCAL_GRACE_MS);
      return;
    }
    e.stopImmediatePropagation();
    send();
  }, true);

  // Enter only submits a field inside a <form>, and generated search pages rarely have
  // one -- so typing a query and pressing Enter did nothing at all. Treated like a
  // data-local click: if the page's own script answers it, fine; if nothing changes,
  // it was a request, and it becomes a turn.
  doc.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    const field = e.target;
    if (!submitsOnEnter(field) || field.form) return;
    const before = doc.body.innerHTML;
    setTimeout(() => {
      if (doc.body.innerHTML !== before) return;
      sendAction({ event: "submit", elementData: describeElement(field), formValues: collectFormState(doc) });
    }, LOCAL_GRACE_MS);
  }, true);

  // Every key pressed in a field, and where -- see KeyLog. Registered on the document
  // rather than the fields, since the fields are replaced every time a region is.
  doc.addEventListener("keydown", (e) => {
    const field = e.target;
    if (e.isComposing || !field.matches?.(FIELDS)) return;
    const symbol = keySymbol(e, field);
    if (symbol) keyLog.key(field, symbol);
  }, true);
  doc.addEventListener("paste", (e) => {
    const field = e.target.closest?.(FIELDS);
    if (field) keyLog.paste(field, e.clipboardData?.getData("text") ?? "");
  }, true);

  // A dropdown or a toggle with no button beside it is the request itself: a filter
  // that only works if the page happened to script it was a filter that did nothing.
  // Same grace as a click, so one the page does handle costs nothing.
  doc.addEventListener("change", (e) => {
    const field = e.target;
    if (!field.matches?.(FIELDS) || !changeIsRequest(field)) return;
    const before = doc.body.innerHTML;
    setTimeout(() => {
      if (doc.body.innerHTML !== before) return;
      sendAction({ event: "change", elementData: describeElement(field), formValues: collectFormState(doc) });
    }, LOCAL_GRACE_MS);
  }, true);

  guardNavigation(doc.defaultView);

  doc.addEventListener("mousemove", (e) => {
    const el = findControl(e.target, doc);
    if (el) predictOnHover(doc, el);
  }, true);

  // Typing invalidates any prediction keyed on the old form values, and the user is
  // clearly mid-thought, so stop burning the GPU on guesses until they settle.
  doc.addEventListener("input", () => { cancelSpeculation(); predictions.clear(); }, true);
}

// A generated page will navigate itself -- location.href to a real search engine, a form
// with an action, window.open's cousin. In the iframe that replaces the app with a
// blank screen (most sites refuse to be framed), and the page being built is lost. What
// the page was trying to show is exactly what the model is for, so the navigation is
// cancelled and becomes a turn. Moving within the page (#anchors) is left alone.
function interceptNavigation(e) {
  if (e.destination.sameDocument || !e.cancelable) return;
  e.preventDefault();
  sendAction({
    event: "navigate",
    elementData: { tag: "navigation", text: "", href: e.destination.url },
    formValues: collectFormState(appEl.contentDocument),
  });
}

function guardNavigation(win) {
  // Re-added on every document.open(), and removed first so it is only ever on once.
  win?.navigation?.removeEventListener("navigate", interceptNavigation);
  win?.navigation?.addEventListener("navigate", interceptNavigation);
}

// Fallback for any document this page did not open itself.
setInterval(() => {
  const doc = appEl.contentDocument;
  if (doc && doc.readyState !== "loading") attachDelegation(doc);
}, 50);

document.getElementById("start-btn").addEventListener("click", startFromConcept);
document.getElementById("concept").addEventListener("keydown", (e) => {
  if (e.key === "Enter") startFromConcept();
});

// --- the address bar -------------------------------------------------------------
//
// The generated app is a website, so it has an address, and the model picks it: a call
// of its own names it before the first screen, and any turn can move it. It only shows
// where the app is; it takes no typing.

const addressEl = document.getElementById("address");
let address = "";

function slugOf(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "app";
}

// What the model wrote, as an address -- or null. A path keeps the current site, and a
// placeholder copied from the prompt ("https://<a short domain>") is not an address.
function toAddress(text, base = address) {
  const raw = text.trim();
  if (!raw || /[<>\s]/.test(raw)) return null;
  try {
    if (/^[/?#]/.test(raw)) return base ? new URL(raw, base).href : null;
    const url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : `https://${raw}`);
    return /^https?:$/.test(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function setAddress(text) {
  const href = toAddress(text);
  if (!href) return;
  address = href;
  // The site in full, the rest of the path quieter -- the way a browser shows it.
  const url = new URL(href);
  const rest = (url.pathname === "/" ? "" : url.pathname) + url.search + url.hash;
  const host = document.createElement("span");
  host.className = "host";
  host.textContent = url.host;
  addressEl.replaceChildren(host, rest);
  addressEl.title = href;
}

// --- home, session and about ------------------------------------------------
//
// Before a session the page is the home screen: the prompt, and the choice of model
// under it. During one it is the generated page, and the same controls sit in the top
// bar -- moved, not copied, so there is still one of each and their listeners come along.

const optionsEl = document.getElementById("options");
const optionsHome = optionsEl.parentElement;
const optionsNext = optionsEl.nextElementSibling;

function enterSession() {
  document.body.classList.remove("at-home");
  document.getElementById("controls-slot").append(optionsEl);
}

function leaveSession() {
  document.body.classList.add("at-home");
  optionsHome.insertBefore(optionsEl, optionsNext);
}

document.getElementById("home-btn").addEventListener("click", () => {
  if (document.body.classList.contains("at-home")) document.getElementById("concept").focus();
  else location.reload();
});

document.getElementById("examples").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-concept]");
  if (!chip) return;
  document.getElementById("concept").value = chip.dataset.concept;
  startFromConcept();
});

// The about page is a panel over whatever is showing, so opening it mid-session loses
// nothing. #about in the address makes it linkable.
const aboutEl = document.getElementById("about");
function showAbout(open) {
  aboutEl.hidden = !open;
  if (open !== (location.hash === "#about")) {
    history.replaceState(null, "", open ? "#about" : location.pathname + location.search);
  }
}
for (const el of document.querySelectorAll("[data-about]")) {
  el.addEventListener("click", () => showAbout(true));
}
document.getElementById("about-close").addEventListener("click", () => showAbout(false));
aboutEl.addEventListener("click", (e) => { if (e.target === aboutEl) showAbout(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !aboutEl.hidden) showAbout(false); });
window.addEventListener("hashchange", () => showAbout(location.hash === "#about"));
showAbout(location.hash === "#about");
if (aboutEl.hidden) document.getElementById("concept").focus();

const engineChosen = setupEngineChoice();
renderTranscript();

// Wiring: pick a model, run turns, apply what comes back, and guess at what is next.
//
// The protocol lives in turn.js and the page handling in dom.js; this is the part that
// knows there is a user watching. Nothing here is specific to where the model runs.

import {
  LOCAL_GRACE_MS, allControls, appendToRegion, applyRegion, applyScreen, collectFormState,
  describeElement, ensureRegions, errorRegion, findControl, isClickable, isLocal,
  openRegion, repairImages, runScripts, splitOversizedRegions,
} from "./dom.js";
import { Journal } from "./journal.js";
import { SHELL_MAX_TOKENS, runPatch, runShell } from "./turn.js";
import {
  createOllamaTransport, createWebLLMTransport, listWebLLMModels, pickWebLLMModel,
  vramBudgetMB, webGPUAvailable,
} from "./transports.js";

const appEl = document.getElementById("app");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");
const bootstrapEl = document.getElementById("bootstrap");
const speculateEl = document.getElementById("speculate");
const engineEl = document.getElementById("engine");
const modelEl = document.getElementById("model");

const delegated = new WeakSet();   // documents whose click/submit delegation is live
let concept = "";
const journal = new Journal();     // what the session has established, compacted as it grows
const log = [];                    // debug transcript
let busy = false;
let transport = null;

function renderTranscript() {
  transcriptEl.textContent = JSON.stringify({
    concept, engine: transport?.label,
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

async function ollamaModels() {
  try {
    const data = await (await fetch("models")).json();
    return data.models ?? [];
  } catch {
    return [];
  }
}

// Auto is the default because the honest answer for most people is "whichever one
// works", and the wrong choice here is a multi-gigabyte download that ends in an
// out-of-memory error.
async function populateModels() {
  const engine = engineEl.value;
  modelEl.replaceChildren(option("", "loading…"));
  modelEl.disabled = true;

  if (engine === "ollama") {
    const models = await ollamaModels();
    modelEl.replaceChildren(...(models.length
      ? models.map((m) => option(m.id, `${m.id} (${(m.sizeMB / 1000).toFixed(1)} GB)`))
      : [option("", "no models pulled")]));
    // Ollama serves one model at a time on a small card; the one already resident is
    // almost always the right default.
    const preferred = models.find((m) => /qwen2\.5-coder/.test(m.id)) ?? models[0];
    if (preferred) modelEl.value = preferred.id;
  } else {
    const [models, budget] = await Promise.all([listWebLLMModels(), vramBudgetMB()]);
    const auto = pickWebLLMModel(models, budget);
    modelEl.replaceChildren(
      option("auto", auto ? `Auto · ${auto.id}` : "Auto"),
      ...models.map((m) => option(
        m.id,
        m.vramMB ? `${m.id} (${(m.vramMB / 1024).toFixed(1)} GB)` : m.id,
        { disabled: m.vramMB !== null && m.vramMB > budget * 1.3 },
      )),
    );
    modelEl.value = "auto";
    modelEl.dataset.auto = auto?.id ?? "";
  }
  modelEl.disabled = false;
}

async function buildTransport() {
  if (engineEl.value === "ollama") {
    return createOllamaTransport({ endpoint: "chat", model: modelEl.value });
  }
  const modelId = modelEl.value === "auto" ? modelEl.dataset.auto : modelEl.value;
  if (!modelId) throw new Error("no in-tab model is available on this device");
  return createWebLLMTransport({ modelId });
}

async function ensureTransportReady() {
  if (!transport) transport = await buildTransport();
  if (!transport.needsPreparing) return;

  loading.show("Loading the model", {
    detail: "First time only — the weights are cached after this.",
  });
  await transport.prepare(({ fraction, text }) => {
    if (fraction > 0) loading.progress(fraction);
    if (text) loading.detail(text);
  });
}

function setupEngineChoice() {
  const options = [option("ollama", "Ollama (this machine)")];
  if (webGPUAvailable()) options.push(option("webllm", "In this tab (WebGPU)"));
  engineEl.replaceChildren(...options);
  // Served as static files with no backend, there is nothing for Ollama to talk to, so
  // in-tab is the only thing that can work.
  engineEl.value = location.protocol === "file:" && webGPUAvailable() ? "webllm" : "ollama";
  engineEl.addEventListener("change", () => { transport = null; populateModels(); });
  modelEl.addEventListener("change", () => { transport = null; });
  return populateModels();
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
      transport, doc, concept, action, memory: journal.toPrompt(), signal: specAbort.signal,
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
  if (!transport) return;
  const myRun = ++specRun;
  if (journal.needsCompaction()) {
    await journal.compact(transport);
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
  if (!speculateEl.checked || busy || isLocal(el) || !transport) return;
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
  if (event.type === "region_open") openRegion(doc, event.id);
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
  if (busy) return;
  busy = true;
  cancelSpeculation();
  const doc = appEl.contentDocument;
  const label = `${action.event} "${(action.elementData.text || "").slice(0, 40)}"`;
  const key = signature(action);

  if (predictions.has(key)) {
    // Already generated while the user was deciding: no model call at all.
    timerStart = performance.now();
    const plan = applyEvents(doc, predictions.get(key));
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

  startTimer("updating");
  loading.show("Updating", { subtle: true, delay: 180 });
  const touched = [];
  let plan = "", written = 0;
  try {
    for await (const event of runPatch({ transport, doc, concept, action, memory: journal.toPrompt() })) {
      if (event.type === "plan") { plan = event.text; timerLabel = plan.slice(0, 80); loading.detail(plan); }
      else if (event.type === "fetching") {
        timerLabel = "fetching live data";
        loading.detail(`Fetching ${new URL(event.url).host}…`);
        loading.indeterminate();
      } else applyEvent(doc, event, touched);
      written += (event.html ?? "").length;
      // No token count to divide by, so progress is measured against the cap the model
      // was given -- roughly right, and always moving forwards.
      if (written) loading.progress(Math.min(0.95, written / 1400));
    }
  } catch (e) {
    applyRegion(doc, "uui-error", errorRegion(e.message));
  } finally {
    loading.hide();
  }
  const secs = stopTimer(touched.length ? ` · changed ${touched.join(", ")}` : " · no change");
  journal.add({ label, plan, inputs: action.formValues });
  log.push({ action: label, plan, touched, secs });
  renderTranscript();
  busy = false;
  speculate(doc);
}

// The first turn is the only one that writes a whole screen. It is streamed into the
// iframe's parser rather than assigned at the end, so the page fills in as it is written.
async function startFromConcept() {
  concept = document.getElementById("concept").value.trim() || "a simple demo app";
  bootstrapEl.style.display = "none";
  busy = true;

  loading.show("Getting ready");
  try {
    await ensureTransportReady();
  } catch (e) {
    loading.hide();
    setStatus(`could not start: ${e.message}`);
    bootstrapEl.style.display = "";
    busy = false;
    return;
  }

  startTimer("building");
  loading.show("Building your app", { detail: concept });
  await baseCssReady;

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
    for await (const event of runShell({ transport, concept })) {
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
  if (!written) doc.write(errorRegion("the model produced nothing"));
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
      // Let the page's own handler run, but never let a real href navigate away.
      if (el.tagName === "A") e.preventDefault();
      const before = doc.body.innerHTML;
      setTimeout(() => {
        if (busy || doc.body.innerHTML !== before) return;
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
      setTimeout(() => { if (!busy && doc.body.innerHTML === before) send(); }, LOCAL_GRACE_MS);
      return;
    }
    e.stopImmediatePropagation();
    send();
  }, true);

  doc.addEventListener("mousemove", (e) => {
    const el = findControl(e.target, doc);
    if (el) predictOnHover(doc, el);
  }, true);

  // Typing invalidates any prediction keyed on the old form values, and the user is
  // clearly mid-thought, so stop burning the GPU on guesses until they settle.
  doc.addEventListener("input", () => { cancelSpeculation(); predictions.clear(); }, true);
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
document.getElementById("reset-btn").addEventListener("click", () => location.reload());

setupEngineChoice();
renderTranscript();

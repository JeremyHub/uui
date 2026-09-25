// The engine: one of it, run in one of two places.
//
// Everything above this file -- prompts, turns, the parser, memory, guessing -- is the
// same code wherever the model runs, and so is everything in this file except the two
// hosts at the bottom. The engine builds the request, streams the reply, stops a
// generation, loads and unloads, and picks a model, once. A host only answers "where":
//
//   listModels()          what can run there, as { id, sizeMB, metered? } -- metered
//                         meaning every call is paid for, so nothing is spent unasked
//   isCached(id)          whether loading needs no download
//   load(id, onProgress)  -> a backend that speaks the OpenAI chat completions shape:
//                            chat.completions.create(), interruptGenerate(), unload()
//
// WebLLM's engine already speaks that shape, so the tab host hands it over as it is.
// The server host speaks it over HTTP, the same way WebLLM's own worker engine speaks it
// over postMessage. Adding a third place would mean writing one more host.

import { WEBLLM_URL, keepGpuJobsShort } from "./gpu-jobs.js";

// --- choosing a model ---------------------------------------------------------

// Below roughly this, a model cannot hold to the reply format at all -- it will not
// keep the #region markers straight, so the app has nothing to apply. Worth picking
// anyway if nothing else fits, but not worth preferring.
const USABLE_FLOOR_MB = 1200;

/**
 * The best model this device can be expected to run, wherever it runs.
 *
 * Largest that fits the budget: quality falls off a cliff at the small end and the
 * protocol asks for structured output. Among equals, prefer the families that follow
 * instructions best at small sizes.
 */
export function pickModel(models, budgetMB) {
  const affordable = models.filter((m) => m.sizeMB !== null && m.sizeMB <= budgetMB);
  if (!affordable.length) return models.find((m) => m.lowResource) ?? models[0] ?? null;

  const rank = (m) => {
    if (/qwen.*coder/i.test(m.id)) return 3;      // best at structured markup for its size
    if (/qwen/i.test(m.id)) return 2;
    if (/llama-?3|phi-?3|gemma-?2/i.test(m.id)) return 1;
    return 0;
  };
  const headroom = budgetMB * 0.9;
  const usable = affordable.filter((m) => m.sizeMB <= headroom && m.sizeMB >= USABLE_FLOOR_MB);
  const shortlist = affordable.filter((m) => m.sizeMB <= headroom);
  const pool = usable.length ? usable : (shortlist.length ? shortlist : affordable);
  return pool.reduce((best, m) => {
    const better = rank(m) - rank(best);
    if (better !== 0) return better > 0 ? m : best;
    return m.sizeMB > best.sizeMB ? m : best;
  });
}

// --- the engine -----------------------------------------------------------------

/** A model, on a host. The same object whichever host it is. */
export function createEngine(host, modelId) {
  let backend = null;
  let loading = null;        // the load in flight, shared by everyone who asks for it
  let reportProgress = null; // whoever asked most recently is the one watching
  let disposed = false;

  async function load() {
    const loaded = await host.load(modelId, (p) => reportProgress?.(p));
    // Dropped while it was loading -- the user picked another model. Let this one go.
    if (disposed) Promise.resolve(loaded.unload?.()).catch(() => {});
    else backend = loaded;
  }

  return {
    host: host.id,
    model: modelId,
    label: `${host.label} · ${modelId}`,
    get ready() { return backend !== null; },

    /** Whether loading needs no download -- which makes it worth starting unasked. */
    async isCached() {
      try { return await host.isCached(modelId); } catch { return false; }
    },

    // Loading can start before anyone is watching (see warmUp in main.js), so a second
    // caller joins the load in flight rather than starting another -- two copies of the
    // weights do not fit on the cards this is aimed at.
    prepare(onProgress) {
      reportProgress = onProgress ?? reportProgress;
      if (backend) return Promise.resolve();
      loading ??= load().finally(() => { loading = null; });
      return loading;
    },

    /** Give the memory back. The next model will not fit alongside this one. */
    dispose() {
      Promise.resolve(backend?.unload?.()).catch(() => {});
      backend = null;
      disposed = true;
    },

    async *chat({ system, user, maxTokens, temperature, signal }) {
      if (!backend) throw new Error("model is not loaded yet");
      const running = backend;
      // Stopping is the backend's job, so an abort frees the model at once rather than
      // after the next token arrives.
      const stop = () => { Promise.resolve(running.interruptGenerate()).catch(() => {}); };
      let stream = null;
      let finished = false;
      if (signal?.aborted) return;
      signal?.addEventListener("abort", stop);
      try {
        stream = (await running.chat.completions.create({
          messages: [
            { role: "system", content: system },
            { role: "user", content: user },
          ],
          stream: true,
          temperature,
          max_tokens: maxTokens,
        }))[Symbol.asyncIterator]();
        while (!signal?.aborted) {
          const { done, value } = await stream.next();
          if (done) { finished = true; break; }
          const piece = value.choices?.[0]?.delta?.content;
          if (piece) yield piece;
        }
      } catch (e) {
        finished = true;
        if (signal?.aborted) return;
        // A lost GPU device takes the engine with it, and every later call fails with
        // "Object has already been disposed" -- a dead app that still looks alive.
        // Forget it, so the next turn loads the model again instead.
        if (/device.*lost|disposed/i.test(e.message ?? "")) {
          if (backend === running) backend = null;
          Promise.resolve(running.unload?.()).catch(() => {});
          throw new Error("The GPU stopped responding, so the model will reload on your next click.");
        }
        throw e instanceof Error ? e : new Error(String(e ?? "the model failed"));
      } finally {
        signal?.removeEventListener("abort", stop);
        // WebLLM holds the engine's lock until a stream runs to its end, and does not
        // release it when one is abandoned -- so a reply cut short (an abort, a #fetch
        // line, a caller that stopped reading) left every later request waiting forever
        // on an idle GPU. Stop the generation and read it out to the end instead.
        if (stream && !finished) {
          await running.interruptGenerate();
          try { while (!(await stream.next()).done); } catch { /* ending anyway */ }
        }
      }
    },
  };
}

// --- where it runs: this machine's server ---------------------------------------

const OLLAMA_KEEP_ALIVE = "30m";

/**
 * The OpenAI chat completions shape, over HTTP to this app's server.
 *
 * The server proxies Ollama's own API, since Ollama does not allow this origin by
 * default, and this is the one place that knows its wire format.
 */
function serverBackend(endpoint, model) {
  const inFlight = new Set();

  async function* completions(request, controller) {
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          model,
          messages: request.messages,
          stream: true,
          keep_alive: OLLAMA_KEEP_ALIVE,
          options: { num_predict: request.max_tokens, temperature: request.temperature },
        }),
      });
      if (!response.ok) {
        throw new Error(`Ollama returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const chunk = JSON.parse(line);
          if (chunk.error) throw new Error(`Ollama error: ${chunk.error}`);
          const content = chunk.message?.content;
          if (content) yield { choices: [{ delta: { content } }] };
        }
      }
    } catch (e) {
      // Interrupted: the generation ends, as WebLLM's does, rather than failing.
      if (!controller.signal.aborted) throw e;
    } finally {
      inFlight.delete(controller);
    }
  }

  return {
    chat: {
      completions: {
        async create(request) {
          const controller = new AbortController();
          inFlight.add(controller);
          return completions(request, controller);
        },
      },
    },
    // Dropping the connection is what stops Ollama generating.
    async interruptGenerate() {
      for (const controller of inFlight) controller.abort();
    },
    async unload() {},
  };
}

export function serverHost({ chatEndpoint = "chat", modelsEndpoint = "models", budgetMB = Infinity } = {}) {
  return {
    id: "server",
    label: "This machine",
    // The server's model is not the tab's to refuse: Ollama can spill a model that does
    // not fit onto the CPU, so the budget steers Auto without ruling anything out.
    budgetMB,
    strictBudget: false,
    warmUp: true,
    loadingDetail: "Starting the model on this machine.",
    async listModels() {
      const response = await fetch(modelsEndpoint);
      if (!response.ok) throw new Error(`the server answered ${response.status}`);
      const data = await response.json();
      return (data.models ?? []).map((m) => ({
        id: m.id, sizeMB: m.sizeMB ?? null, metered: Boolean(m.metered),
      }));
    },
    async isCached() { return true; },
    async load(modelId) { return serverBackend(chatEndpoint, modelId); },
  };
}

// --- where it runs: this tab ------------------------------------------------------

let webllmModule = null;

async function loadWebLLM() {
  if (!webllmModule) webllmModule = await import(/* @vite-ignore */ WEBLLM_URL);
  return webllmModule;
}

/**
 * Whether a model can actually run here, and how much of one.
 *
 * `"gpu" in navigator` is not the question: the property exists on machines where
 * requestAdapter() then returns null, and offering in-tab inference there means a user
 * picks it and waits for a multi-gigabyte download that cannot work. Asking for the
 * adapter is the only honest test.
 *
 * The budget comes from WebGPU's buffer limits rather than deviceMemory, which is system
 * RAM, rounded, and capped at 8 by every browser that reports it at all.
 *
 * maxBufferSize is taken as the budget directly. It is not a VRAM figure -- it is a
 * per-buffer cap -- but on the cards this was checked against it lands close: a 4GB
 * RX 570 reports 4GB, and Chrome's software fallback reports 1GB. Scaling it up, which
 * an earlier version did, turned that 4GB card into a 16GB budget and would have picked
 * a model that downloads for several minutes and then fails to allocate. The two
 * mistakes are not symmetric: too small is a working app with a weaker model, too large
 * is a long wait ending in nothing.
 */
const MAX_SENSIBLE_BUDGET_MB = 8192;

/** How much model to try to hold, given what the adapter reports. */
export function budgetFromLimits(limits = {}) {
  const perBufferMB = Math.max(
    (limits.maxBufferSize ?? 0) / (1024 * 1024),
    (limits.maxStorageBufferBindingSize ?? 0) / (1024 * 1024),
  );
  if (perBufferMB <= 0) return 2000;
  return Math.min(Math.round(perBufferMB), MAX_SENSIBLE_BUDGET_MB);
}

export async function webGPUCapability() {
  if (typeof navigator === "undefined" || !("gpu" in navigator)) {
    return { ok: false, budgetMB: 0, why: "This browser has no WebGPU." };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) {
      return { ok: false, budgetMB: 0, why: "WebGPU is present but no adapter is available." };
    }
    // Chrome falls back to SwiftShader -- a CPU rasterizer -- when it cannot reach the
    // GPU, and reports it as a perfectly good adapter. It does work, at something like a
    // hundredth of the speed: a first screen that takes 20 seconds on this card did not
    // finish in fifteen minutes on SwiftShader. Worth saying out loud rather than
    // letting someone conclude the app is broken.
    const info = adapter.info ?? {};
    const software = /swiftshader|lavapipe|llvmpipe|software/i.test(
      `${info.architecture ?? ""} ${info.description ?? ""} ${info.vendor ?? ""}`,
    );

    return {
      ok: true,
      software,
      budgetMB: budgetFromLimits(adapter.limits),
      // Half the prebuilt models are f16 quantised and simply refuse to start without
      // this extension. Finding that out costs a gigabyte-scale download first, so it
      // is worth asking the adapter up front.
      f16: Boolean(adapter.features?.has?.("shader-f16")),
    };
  } catch (e) {
    return { ok: false, budgetMB: 0, why: `WebGPU could not start (${e.message}).` };
  }
}

/** The prebuilt entries this device could actually run, smallest first. */
export function usableModels(entries, { f16 = true } = {}) {
  return entries
    .map((m) => ({
      id: m.model_id,
      sizeMB: m.vram_required_MB ?? null,
      lowResource: Boolean(m.low_resource_required),
      needsF16: /f16/i.test(m.model_id),
    }))
    // Instruct-tuned only: a base model cannot follow the reply format at all.
    .filter((m) => /instruct|-it-|chat|hermes/i.test(m.id))
    // Offering a model this device will refuse to start means the refusal arrives after
    // the download, which is the most expensive way possible to learn it.
    .filter((m) => f16 || !m.needsF16)
    .sort((a, b) => (a.sizeMB ?? 1e9) - (b.sizeMB ?? 1e9));
}

// How many prompt tokens go through the model in one GPU job. The prebuilt configs say
// 2048, which is the whole first-turn prompt at once -- several seconds of GPU time on a
// mid-range card, past the point where the driver resets the GPU. See gpu-jobs.js.
const PREFILL_CHUNK_TOKENS = 128;

export function tabHost(gpu, { prefillChunkTokens = PREFILL_CHUNK_TOKENS, useWorker = true } = {}) {
  return {
    id: "tab",
    label: gpu.software ? "In this tab (CPU only — very slow)" : "In this tab",
    budgetMB: gpu.budgetMB,
    // A model too big for the card fails to allocate after a multi-gigabyte download.
    strictBudget: true,
    // Nothing uncached is fetched unasked, and on a software adapter even a cached
    // load is a long wait nobody asked for.
    warmUp: !gpu.software,
    loadingDetail: gpu.software
      ? "Your browser is running WebGPU on the CPU, not the graphics card, so this will be very slow."
      : "First time only — the weights are cached after this.",

    async listModels() {
      const webllm = await loadWebLLM();
      return usableModels(webllm.prebuiltAppConfig?.model_list ?? [], { f16: gpu.f16 });
    },

    async isCached(modelId) {
      return (await loadWebLLM()).hasModelInCache(modelId);
    },

    async load(modelId, onProgress) {
      const webllm = await loadWebLLM();
      const config = {
        initProgressCallback: (report) => {
          // report.progress is 0..1 over the whole load; text says which shard.
          onProgress?.({ fraction: report.progress ?? 0, text: report.text ?? "" });
        },
      };
      // In a worker when the page is served, so decoding does not queue behind the page
      // rendering what it just decoded -- see llm-worker.js. A file:// page cannot start
      // a worker at all, and there the main thread is still better than nothing.
      let worker = null;
      try {
        if (useWorker) worker = new Worker(new URL("./llm-worker.js", import.meta.url), { type: "module" });
      } catch (e) {
        console.warn("no worker for the model, running it on the page's thread", e);
      }
      const chatOptions = { prefill_chunk_size: prefillChunkTokens };
      let engine;
      if (worker) {
        engine = await webllm.CreateWebWorkerMLCEngine(worker, modelId, config, chatOptions);
      } else {
        engine = await webllm.CreateMLCEngine(modelId, config, chatOptions);
        keepGpuJobsShort(engine, prefillChunkTokens);
      }
      // WebLLM's engine is already the backend shape. Unloading also ends the worker,
      // which is what actually hands the GPU memory back.
      const unload = engine.unload.bind(engine);
      engine.unload = async () => {
        try { await unload(); } finally { worker?.terminate(); }
      };
      return engine;
    },
  };
}

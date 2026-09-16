// Where the tokens come from.
//
// The turn protocol does not care whether a model is running on this machine behind a
// server or inside this tab on the GPU, so both are behind one interface: give it a
// system and user message, get an async iterable of text back. Adding a third would mean
// writing one object.
//
//   prepare(onProgress)  -> resolves when the model can answer. Instant for a server,
//                           a multi-gigabyte download the first time for in-tab.
//   chat({...})          -> async iterable of text pieces.

const OLLAMA_KEEP_ALIVE = "30m";

/** Ollama, reached through this app's own server (or directly, if it allows the origin). */
export function createOllamaTransport({ endpoint = "/chat", model }) {
  return {
    id: "ollama",
    model,
    label: `Ollama · ${model}`,
    needsPreparing: false,
    async prepare() {},

    async *chat({ system, user, maxTokens, temperature, signal }) {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal,
        body: JSON.stringify({
          model,
          messages: [
            { role: "system", content: system },
            { role: "user", content: user },
          ],
          stream: true,
          keep_alive: OLLAMA_KEEP_ALIVE,
          options: { num_predict: maxTokens, temperature },
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
          const piece = chunk.message?.content;
          if (piece) yield piece;
        }
      }
    },
  };
}

// --- in-tab inference -------------------------------------------------------

const WEBLLM_CDN = "https://esm.run/@mlc-ai/web-llm";
let webllmModule = null;

async function loadWebLLM() {
  if (!webllmModule) webllmModule = await import(/* @vite-ignore */ WEBLLM_CDN);
  return webllmModule;
}

export function webGPUAvailable() {
  return typeof navigator !== "undefined" && "gpu" in navigator;
}

/**
 * How much model this device can be asked to hold, in MB.
 *
 * WebGPU reports buffer limits rather than total VRAM, and the largest single buffer is
 * what actually caps a model here, so it is a better guide than deviceMemory -- which is
 * system RAM, rounded, and capped at 8 on every browser that reports it at all. Falls
 * back to a deliberately timid number: picking a model too large means a long download
 * that ends in an out-of-memory error, which is far worse than picking a small one.
 */
export async function vramBudgetMB() {
  if (!webGPUAvailable()) return 0;
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) return 0;
    const maxBufferMB = (adapter.limits?.maxBufferSize ?? 0) / (1024 * 1024);
    const maxStorageMB = (adapter.limits?.maxStorageBufferBindingSize ?? 0) / (1024 * 1024);
    const reported = Math.max(maxBufferMB, maxStorageMB);
    // Limits are per-buffer, not a total, and weights are split across many buffers, so
    // the total a device will take is some multiple of this. 4x is conservative.
    return reported > 0 ? Math.round(reported * 4) : 2000;
  } catch {
    return 2000;
  }
}

/** The prebuilt models, smallest first, annotated with what we can tell about them. */
export async function listWebLLMModels() {
  const webllm = await loadWebLLM();
  const models = webllm.prebuiltAppConfig?.model_list ?? [];
  return models
    .map((m) => ({
      id: m.model_id,
      vramMB: m.vram_required_MB ?? null,
      lowResource: Boolean(m.low_resource_required),
    }))
    // Instruct-tuned only: a base model cannot follow the reply format at all.
    .filter((m) => /instruct|-it-|chat|hermes/i.test(m.id))
    .sort((a, b) => (a.vramMB ?? 1e9) - (b.vramMB ?? 1e9));
}

/**
 * The best model this device can be expected to run.
 *
 * Largest that fits the budget, because quality falls off a cliff below about 1B and the
 * protocol asks for structured output that tiny models cannot hold to. Among equals,
 * prefer the families that follow instructions best at small sizes.
 */
export function pickWebLLMModel(models, budgetMB) {
  const affordable = models.filter((m) => m.vramMB !== null && m.vramMB <= budgetMB);
  if (!affordable.length) return models.find((m) => m.lowResource) ?? models[0] ?? null;

  const rank = (m) => {
    if (/qwen.*coder/i.test(m.id)) return 3;      // best at structured markup for its size
    if (/qwen/i.test(m.id)) return 2;
    if (/llama-3|phi-3|gemma-2/i.test(m.id)) return 1;
    return 0;
  };
  const headroom = budgetMB * 0.9;
  const shortlist = affordable.filter((m) => m.vramMB <= headroom);
  const pool = shortlist.length ? shortlist : affordable;
  return pool.reduce((best, m) => {
    const better = rank(m) - rank(best);
    if (better !== 0) return better > 0 ? m : best;
    return m.vramMB > best.vramMB ? m : best;
  });
}

/** A model running in this tab. No server, no network after the first load. */
export async function createWebLLMTransport({ modelId }) {
  const webllm = await loadWebLLM();
  let engine = null;

  return {
    id: "webllm",
    model: modelId,
    label: `In this tab · ${modelId}`,
    needsPreparing: true,

    async prepare(onProgress) {
      if (engine) return;
      engine = await webllm.CreateMLCEngine(modelId, {
        initProgressCallback: (report) => {
          // report.progress is 0..1 over the whole load; text says which shard.
          onProgress?.({ fraction: report.progress ?? 0, text: report.text ?? "" });
        },
      });
    },

    async *chat({ system, user, maxTokens, temperature, signal }) {
      if (!engine) throw new Error("model is not loaded yet");
      const stream = await engine.chat.completions.create({
        messages: [
          { role: "system", content: system },
          { role: "user", content: user },
        ],
        stream: true,
        temperature,
        max_tokens: maxTokens,
      });
      for await (const chunk of stream) {
        if (signal?.aborted) {
          // Nothing else is queued behind this, and the engine is single-threaded, so
          // stopping the generation is what frees it for the click the user just made.
          await engine.interruptGenerate();
          return;
        }
        const piece = chunk.choices?.[0]?.delta?.content;
        if (piece) yield piece;
      }
    },
  };
}

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

/**
 * Whether a model can actually run here, and how much of one.
 *
 * `"gpu" in navigator` is not the question: the property exists on machines where
 * requestAdapter() then returns null, and offering in-tab inference there means a user
 * picks it and waits for a multi-gigabyte download that cannot work. Asking for the
 * adapter is the only honest test.
 *
 * The budget comes from WebGPU's buffer limits rather than deviceMemory, which is system
 * RAM, rounded, and capped at 8 by every browser that reports it at all. Limits are
 * per-buffer and weights are spread across many, so the total a device will take is a
 * multiple of it; 4x is deliberately timid, because picking a model too large means a
 * long download that ends in an out-of-memory error.
 */
export async function webGPUCapability() {
  if (typeof navigator === "undefined" || !("gpu" in navigator)) {
    return { ok: false, budgetMB: 0, why: "This browser has no WebGPU." };
  }
  try {
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) {
      return { ok: false, budgetMB: 0, why: "WebGPU is present but no adapter is available." };
    }
    const perBufferMB = Math.max(
      (adapter.limits?.maxBufferSize ?? 0) / (1024 * 1024),
      (adapter.limits?.maxStorageBufferBindingSize ?? 0) / (1024 * 1024),
    );
    return { ok: true, budgetMB: perBufferMB > 0 ? Math.round(perBufferMB * 4) : 2000 };
  } catch (e) {
    return { ok: false, budgetMB: 0, why: `WebGPU could not start (${e.message}).` };
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

// Below roughly this, a model cannot hold to the reply format at all -- it will not
// keep the #region markers straight, so the app has nothing to apply. Worth picking
// anyway if nothing else fits, but not worth preferring.
const USABLE_FLOOR_MB = 1200;

/**
 * The best model this device can be expected to run.
 *
 * Largest that fits the budget: quality falls off a cliff at the small end and the
 * protocol asks for structured output. Among equals, prefer the families that follow
 * instructions best at small sizes.
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
  const usable = affordable.filter((m) => m.vramMB <= headroom && m.vramMB >= USABLE_FLOOR_MB);
  const shortlist = affordable.filter((m) => m.vramMB <= headroom);
  const pool = usable.length ? usable : (shortlist.length ? shortlist : affordable);
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

// Keeping each GPU job short enough that the driver does not reset the GPU.
//
// Linux's amdgpu driver resets the GPU when one job runs longer than its lockup timeout
// -- two seconds by default on current kernels. That is not an error the tab can catch:
// the display runs on the same GPU, so the screen goes black, and on the RX 570 this
// was measured on it took the whole session down once. When the reset is "soft", the
// job is killed and whatever it was computing is garbage -- a model that echoes its own
// prompt back -- before WebGPU reports the device lost.
//
// WebLLM splits a long prompt into chunks, but two things stop that from helping. The
// chunk size comes from the compiled model -- 1024 tokens for Qwen2.5, over two seconds
// on the RX 570 by itself -- and ignores the chat option meant to set it. And the runtime
// underneath records every dispatch into one pending command buffer, submitted only when
// something reads a result back, which prefill does not do until the last chunk. So both
// are fixed here: a smaller chunk, and a wait after each one so it is its own job.
//
// This reaches into WebLLM's pipeline, so the version is pinned in WEBLLM_URL: a
// different release may rename what is wrapped here. If the names are not found it does
// nothing, which is the behaviour before it existed.

export const WEBLLM_URL = "https://esm.run/@mlc-ai/web-llm@0.2.85";

/** The parts of WebLLM's LLMChatPipeline this reaches into. Not public API. */
interface Pipeline {
  prefillChunkSize: number;
  embedAndForward?: (inputs: unknown, length: number, ...rest: unknown[]) => Promise<unknown>;
  device?: { sync?: () => Promise<void> };
}

/** An MLCEngine, as far as finding its pipelines goes. */
interface PipelineOwner {
  loadedModelIdToPipeline?: Map<string, Pipeline>;
}

const wrapped = new WeakSet<Pipeline>();

function syncEachChunk(pipeline: Pipeline | undefined, chunkTokens: number): void {
  if (!pipeline || wrapped.has(pipeline)) return;
  if (typeof pipeline.embedAndForward !== "function" || typeof pipeline.device?.sync !== "function") {
    console.warn("uui: WebLLM internals changed; long prompts may trip the GPU watchdog");
    return;
  }
  wrapped.add(pipeline);
  // Read from the compiled model rather than the chat config, so the prefill_chunk_size
  // chat option does nothing. The KV cache was sized for the compiled value; a smaller
  // chunk fits inside it.
  if (chunkTokens > 0) pipeline.prefillChunkSize = Math.min(pipeline.prefillChunkSize, chunkTokens);
  const forward = pipeline.embedAndForward.bind(pipeline);
  // Only prompt chunks. A decoded token is one position, far inside the limit, and its
  // result is read back straight away regardless.
  pipeline.embedAndForward = async (inputs: unknown, length: number, ...rest: unknown[]) => {
    const result = await forward(inputs, length, ...rest);
    if (length > 1) await pipeline.device!.sync!();
    return result;
  };
}

/** Patch every model this engine has loaded. Call after each load. */
export function keepGpuJobsShort(engine: object | null | undefined, chunkTokens: number): void {
  for (const pipeline of (engine as PipelineOwner | null | undefined)?.loadedModelIdToPipeline?.values?.() ?? []) {
    syncEachChunk(pipeline, chunkTokens);
  }
}

// The in-tab model, off the page's thread.
//
// Decoding is a loop of tiny GPU submissions, one per token, each waiting on the last
// token's result before the next can be queued. On the page's thread that wait also
// waits behind everything else the page does between tokens -- and the first screen is
// streamed into the iframe's parser as it is written, so every token paid for a parse,
// a style pass over base.css's :has() rules and a layout before the GPU saw the next
// submission. Here the loop runs back to back and the page renders in parallel with it.

// Imported statically, and so spelled out rather than taken from WEBLLM_URL: the page
// posts the load request the moment this starts, and a top-level await here would let
// it arrive before onmessage exists. Must match WEBLLM_URL in gpu-jobs.ts.
import { WebWorkerMLCEngineHandler, type ChatOptions } from "https://esm.run/@mlc-ai/web-llm@0.2.85";
import { keepGpuJobsShort } from "./gpu-jobs.js";

const handler = new WebWorkerMLCEngineHandler();

// Loading goes through reload(), and the pipeline it builds is only reachable afterwards.
const reload = handler.engine.reload.bind(handler.engine);
// The page's options arrive here as one per model, in an array.
handler.engine.reload = async (modelIds: string | string[], chatOptions?: ChatOptions | ChatOptions[]) => {
  await reload(modelIds, chatOptions);
  const options = [chatOptions ?? []].flat()[0];
  keepGpuJobsShort(handler.engine, options?.prefill_chunk_size ?? 0);
};

self.onmessage = (message: MessageEvent) => handler.onmessage(message);

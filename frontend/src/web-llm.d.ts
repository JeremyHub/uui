// WebLLM is imported straight from a CDN, pinned in WEBLLM_URL (gpu-jobs.ts), so there is
// no package for the types to come from. This is the part of its API the app uses, and
// nothing more. Keep the version in the module name in step with WEBLLM_URL.

declare module "https://esm.run/@mlc-ai/web-llm@0.2.85" {
  export interface ModelRecord {
    model_id: string;
    vram_required_MB?: number;
    low_resource_required?: boolean;
  }

  export interface InitProgressReport {
    progress?: number;
    text?: string;
  }

  export interface MLCEngineConfig {
    initProgressCallback?: (report: InitProgressReport) => void;
  }

  export interface ChatOptions {
    prefill_chunk_size?: number;
  }

  export interface ChatCompletionRequest {
    messages: { role: "system" | "user" | "assistant"; content: string }[];
    stream: true;
    temperature?: number;
    max_tokens?: number;
  }

  export interface ChatCompletionChunk {
    choices?: { delta?: { content?: string | null } }[];
  }

  export interface MLCEngineInterface {
    chat: {
      completions: {
        create(request: ChatCompletionRequest): Promise<AsyncIterable<ChatCompletionChunk>>;
      };
    };
    interruptGenerate(): void | Promise<void>;
    unload(): Promise<void>;
    reload(modelId: string | string[], chatOpts?: ChatOptions | ChatOptions[]): Promise<void>;
  }

  export const prebuiltAppConfig: { model_list: ModelRecord[] } | undefined;

  export function hasModelInCache(modelId: string): Promise<boolean>;

  export function CreateMLCEngine(
    modelId: string, engineConfig?: MLCEngineConfig, chatOpts?: ChatOptions,
  ): Promise<MLCEngineInterface>;

  export function CreateWebWorkerMLCEngine(
    worker: Worker, modelId: string, engineConfig?: MLCEngineConfig, chatOpts?: ChatOptions,
  ): Promise<MLCEngineInterface>;

  export class WebWorkerMLCEngineHandler {
    engine: MLCEngineInterface;
    onmessage(event: MessageEvent): void;
  }
}

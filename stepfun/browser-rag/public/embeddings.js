import { env, pipeline } from "/node_modules/@huggingface/transformers/dist/transformers.min.js";

export const MODEL_ID = "Xenova/bge-small-zh-v1.5";
export const MODEL_REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781";
export const DIMENSION = 512;
const QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章：";

env.allowLocalModels = true;
env.allowRemoteModels = false;
env.localModelPath = "/models/";
env.useBrowserCache = true;
env.backends.onnx.wasm.wasmPaths = "/node_modules/onnxruntime-web/dist/";

let loading = null;
let active = null;

async function createExtractor(device, dtype, onProgress) {
  const started = performance.now();
  const extractor = await pipeline("feature-extraction", MODEL_ID, {
    revision: MODEL_REVISION,
    device,
    dtype,
    progress_callback: onProgress,
  });
  await extractor(["模型预热"], { pooling: "cls", normalize: true });
  return { extractor, device, dtype, loadMs: performance.now() - started };
}

export function backendStatus() {
  return active ? { device: active.device, dtype: active.dtype, loadMs: active.loadMs } : null;
}

export function loadModel(onProgress = () => {}, forcedBackend = null) {
  if (!loading) {
    loading = (async () => {
      if (forcedBackend) {
        active = await createExtractor(forcedBackend.device, forcedBackend.dtype, onProgress);
        return active;
      }
      if (navigator.gpu) {
        for (const dtype of ["fp16", "fp32"]) {
          try {
            active = await createExtractor("webgpu", dtype, onProgress);
            return active;
          } catch (error) {
            console.warn(`WebGPU ${dtype} initialization failed`, error);
          }
        }
      }
      active = await createExtractor("wasm", "q8", onProgress);
      return active;
    })().catch((error) => {
      loading = null;
      active = null;
      throw error;
    });
  }
  return loading;
}

export async function embedTexts(texts, { query = false, batchSize = 8 } = {}) {
  if (!Array.isArray(texts) || texts.length === 0 || texts.some((text) => typeof text !== "string")) {
    throw new Error("embedding input must be a non-empty string array");
  }
  if (texts.length > 64) throw new Error("embedding batch exceeds 64 texts");
  const { extractor } = await loadModel();
  const input = query ? texts.map((text) => QUERY_PREFIX + text) : texts;
  const started = performance.now();
  const tensor = await extractor(input, { pooling: "cls", normalize: true, batch_size: batchSize });
  const embeddings = tensor.tolist();
  if (embeddings.length !== texts.length || embeddings.some((vector) => vector.length !== DIMENSION)) {
    throw new Error("model returned an unexpected vector shape");
  }
  return { embeddings, elapsedMs: performance.now() - started };
}

export function chunkText(text, maxLength = 700, overlap = 70) {
  if (typeof text !== "string" || !text.trim()) return [];
  const paragraphs = text.replaceAll("\r\n", "\n").split(/\n{2,}/).map((part) => part.trim()).filter(Boolean);
  const pieces = [];
  for (const paragraph of paragraphs) {
    for (let start = 0; start < paragraph.length; start += maxLength - overlap) {
      pieces.push(paragraph.slice(start, start + maxLength));
      if (start + maxLength >= paragraph.length) break;
    }
  }
  return pieces;
}

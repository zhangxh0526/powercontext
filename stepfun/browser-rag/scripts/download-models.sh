#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE='https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b069aac4d136ba6bc1122f995fedcfd2781'
DEST="$ROOT/public/models/Xenova/bge-small-zh-v1.5"
mkdir -p "$DEST/onnx"
FILES=(
  config.json
  tokenizer.json
  tokenizer_config.json
  vocab.txt
  special_tokens_map.json
  onnx/model.onnx
  onnx/model_fp16.onnx
  onnx/model_quantized.onnx
)
for file in "${FILES[@]}"; do
  curl -fsSL --retry 2 --max-time 300 "$BASE/$file" -o "$DEST/$file"
  wc -c "$DEST/$file"
done

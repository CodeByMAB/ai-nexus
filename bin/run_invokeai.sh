#!/bin/bash
set -euo pipefail
cd ${HOME}/invokeai

export CUDA_VISIBLE_DEVICES=0
export PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:2048,garbage_collection_threshold:0.8,expandable_segments:True

export INVOKEAI_ROOT=${HOME}/invokeai
export INVOKEAI_HOST=127.0.0.1
export INVOKEAI_PORT=7860

source /opt/projects/invokeai-env/bin/activate
exec invokeai-web --root "$INVOKEAI_ROOT"

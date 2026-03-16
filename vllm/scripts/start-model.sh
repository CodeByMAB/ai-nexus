#!/bin/bash
# Start vLLM with a specific model.
# Usage:
#   ./start-model.sh [extreme|code|fast|fast+image]
#   ./start-model.sh --print-execstart [extreme|code|fast|fast+image]

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  start-model.sh [extreme|code|fast|fast+image]
  start-model.sh --print-execstart [extreme|code|fast|fast+image]
  start-model.sh --dry-run [extreme|code|fast|fast+image]
EOF
}

MODE="fast"
PRINT_EXECSTART="false"
DRY_RUN="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        extreme|code|fast|fast+image)
            MODE="$1"
            shift
            ;;
        --print-execstart)
            PRINT_EXECSTART="true"
            shift
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

VLLM_DIR="${AI_ROOT:-${AI_ROOT:-/opt/ai}}/vllm"
LOG_DIR="$VLLM_DIR/logs"
EXTREME_LOCAL_FP8_DEFAULT="${MODELS_ROOT:-/opt/models}/Mistral-Small-3.2-24B-FP8"
EXTREME_SOURCE_HF_DIR="${HF_HOME:-${MODELS_ROOT:-/opt/models}/huggingface}/hub/models--mistralai--Mistral-Small-3.2-24B-Instruct-2506"

resolve_latest_snapshot_dir() {
    local src_dir="$1"
    local snaps_dir="$src_dir/snapshots"
    if [[ ! -d "$snaps_dir" ]]; then
        return 1
    fi
    local latest
    latest="$(ls -1 "$snaps_dir" 2>/dev/null | sort | tail -n1 || true)"
    if [[ -z "$latest" ]]; then
        return 1
    fi
    printf '%s\n' "$snaps_dir/$latest"
}

is_valid_local_checkpoint_dir() {
    local model_dir="$1"
    if [[ ! -d "$model_dir" || ! -f "$model_dir/config.json" ]]; then
        return 1
    fi

    shopt -s nullglob
    local consolidated_files=("$model_dir"/consolidated*.safetensors)
    local hf_shard_files=("$model_dir"/model-*.safetensors)
    shopt -u nullglob

    if (( ${#consolidated_files[@]} > 0 )) || [[ -f "$model_dir/consolidated.safetensors" ]]; then
        return 0
    fi
    if (( ${#hf_shard_files[@]} > 0 )) || [[ -f "$model_dir/model.safetensors" ]] || [[ -f "$model_dir/model.safetensors.index.json" ]]; then
        return 0
    fi
    return 1
}

build_mode_profile() {
    QUANTIZATION=""
    KV_CACHE_DTYPE=""
    SWAP_SPACE=""
    CPU_OFFLOAD_GB=""
    KV_OFFLOADING_SIZE=""
    KV_OFFLOADING_BACKEND=""
    DISABLE_HYBRID_KV_CACHE_MANAGER="false"
    MAX_NUM_SEQS=""
    MAX_NUM_BATCHED_TOKENS=""
    ATTENTION_BACKEND=""
    ENFORCE_EAGER=""
    CALCULATE_KV_SCALES="false"
    GPU_MEMORY_UTILIZATION="0.95"
    PYTORCH_CUDA_ALLOC_CONF_VALUE=""
    SERVED_MODEL_NAME="$MODE"
    TOKENIZER_MODE="mistral"
    CONFIG_FORMAT="mistral"
    LOAD_FORMAT="mistral"

    case "$1" in
        extreme)
            # Prefer a locally converted FP8 checkpoint when present.
            # Override with VLLM_EXTREME_MODEL to force a model path/ID.
            local source_snapshot_default=""
            local local_fp8_default_valid="false"
            source_snapshot_default="$(resolve_latest_snapshot_dir "$EXTREME_SOURCE_HF_DIR" || true)"
            if is_valid_local_checkpoint_dir "$EXTREME_LOCAL_FP8_DEFAULT"; then
                local_fp8_default_valid="true"
            fi
            if [[ -n "${VLLM_EXTREME_MODEL:-}" ]]; then
                MODEL="${VLLM_EXTREME_MODEL}"
            elif [[ "${VLLM_EXTREME_USE_LOCAL_FP8:-0}" == "1" && "$local_fp8_default_valid" == "true" ]]; then
                MODEL="$EXTREME_LOCAL_FP8_DEFAULT"
            elif [[ -n "$source_snapshot_default" ]]; then
                # Use HF BF16 source — vLLM applies FP8 quantization on-the-fly
                MODEL="$source_snapshot_default"
            else
                MODEL="$EXTREME_LOCAL_FP8_DEFAULT"
            fi
            # With BF16 KV cache on 32GB cards, 32768 can be slightly above
            # available KV budget after model load. Use a safer default.
            MAX_LEN="${VLLM_MAX_LEN_EXTREME:-30720}"
            QUANTIZATION="fp8"
            KV_CACHE_DTYPE="${VLLM_KV_CACHE_DTYPE_EXTREME:-auto}"
            SWAP_SPACE="${VLLM_SWAP_SPACE_EXTREME:-8}"
            # Keep weights on GPU by default for faster decode.
            # Set VLLM_CPU_OFFLOAD_EXTREME>0 only if memory pressure requires it.
            CPU_OFFLOAD_GB="${VLLM_CPU_OFFLOAD_EXTREME:-0}"
            # Optional KV offloading to system RAM (GiB).
            # Default OFF — native KV offloading causes severe quality degradation
            # with TRITON_ATTN + FP8 weights on RTX 50-series (produces gibberish
            # even at very short context lengths).
            # Set VLLM_KV_OFFLOADING_SIZE_EXTREME>0 only after verifying output quality.
            KV_OFFLOADING_SIZE="${VLLM_KV_OFFLOADING_SIZE_EXTREME:-0}"
            KV_OFFLOADING_BACKEND="${VLLM_KV_OFFLOADING_BACKEND_EXTREME:-native}"
            if [[ -n "$KV_OFFLOADING_SIZE" && "$KV_OFFLOADING_SIZE" != "0" ]]; then
                if [[ "${VLLM_DISABLE_HYBRID_KV_CACHE_MANAGER_EXTREME:-1}" == "1" || \
                      "${VLLM_DISABLE_HYBRID_KV_CACHE_MANAGER_EXTREME:-}" == "true" || \
                      "${VLLM_DISABLE_HYBRID_KV_CACHE_MANAGER_EXTREME:-}" == "TRUE" ]]; then
                    DISABLE_HYBRID_KV_CACHE_MANAGER="true"
                fi
            fi
            MAX_NUM_SEQS=1
            MAX_NUM_BATCHED_TOKENS=2048
            ATTENTION_BACKEND="${VLLM_ATTENTION_BACKEND_EXTREME:-TRITON_ATTN}"
            # fp8 KV cache without calibrated scales can cause severe quality issues.
            # NOTE: TRITON_ATTN currently asserts when q_scale != 1.0, so keep
            # dynamic kv-scale calculation disabled by default on this backend.
            if [[ "$KV_CACHE_DTYPE" == fp8* ]]; then
                CALCULATE_KV_SCALES="${VLLM_CALCULATE_KV_SCALES_EXTREME:-false}"
                if [[ "$ATTENTION_BACKEND" == "TRITON_ATTN" ]] && \
                   [[ "$CALCULATE_KV_SCALES" == "1" || "$CALCULATE_KV_SCALES" == "true" || "$CALCULATE_KV_SCALES" == "TRUE" ]]; then
                    echo "Warning: --calculate-kv-scales is incompatible with TRITON_ATTN; disabling it." >&2
                    CALCULATE_KV_SCALES="false"
                fi
            fi
            # Eager mode disables cudagraph/compile optimizations.
            # Required with TRITON_ATTN — CUDA graphs corrupt attention output
            # and cause gibberish/repetitive text at longer context lengths.
            ENFORCE_EAGER="true"
            GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION_EXTREME:-0.95}"
            PYTORCH_CUDA_ALLOC_CONF_VALUE="expandable_segments:True"

            # If a local HF-style checkpoint is provided (model-*.safetensors /
            # model.safetensors.index.json), prefer auto loaders (Infermatic flow).
            if [[ "$MODEL" == /* && -d "$MODEL" ]]; then
                shopt -s nullglob
                local consolidated_files=("$MODEL"/consolidated*.safetensors)
                local hf_shard_files=("$MODEL"/model-*.safetensors)
                shopt -u nullglob

                if (( ${#hf_shard_files[@]} > 0 )) || [[ -f "$MODEL/model.safetensors" ]] || [[ -f "$MODEL/model.safetensors.index.json" ]]; then
                    if [[ "${VLLM_EXTREME_PREFER_HF_AUTO_FORMAT:-1}" == "1" ]]; then
                        LOAD_FORMAT="auto"
                        CONFIG_FORMAT="auto"
                        TOKENIZER_MODE="${VLLM_TOKENIZER_MODE_EXTREME_HF:-auto}"
                    fi
                elif (( ${#consolidated_files[@]} == 0 )) && [[ ! -f "$MODEL/consolidated.safetensors" ]]; then
                    # No known local file pattern; leave explicit formats in place.
                    :
                fi
            fi
            ;;
        code)
            MODEL="mistralai/Devstral-Small-2-24B-Instruct-2512"
            MAX_LEN="${VLLM_MAX_LEN_CODE:-65536}"
            QUANTIZATION="fp8"
            KV_CACHE_DTYPE="auto"
            SWAP_SPACE="${VLLM_SWAP_SPACE_CODE:-14}"
            CPU_OFFLOAD_GB="${VLLM_CPU_OFFLOAD_CODE:-0}"
            MAX_NUM_SEQS=1
            MAX_NUM_BATCHED_TOKENS=2048
            ATTENTION_BACKEND="TRITON_ATTN"
            ENFORCE_EAGER="true"
            ;;
        fast)
            MODEL="mistralai/Ministral-3-14B-Instruct-2512"
            MAX_LEN="${VLLM_MAX_LEN_FAST:-85264}"
            KV_CACHE_DTYPE="${VLLM_KV_CACHE_DTYPE_FAST:-auto}"
            SWAP_SPACE="${VLLM_SWAP_SPACE_FAST:-8}"
            CPU_OFFLOAD_GB="${VLLM_CPU_OFFLOAD_FAST:-0}"
            MAX_NUM_SEQS=4
            MAX_NUM_BATCHED_TOKENS=8192
            ATTENTION_BACKEND="TRITON_ATTN"
            ENFORCE_EAGER="true"
            ;;
        fast+image)
            MODEL="mistralai/Ministral-3-14B-Instruct-2512"
            MAX_LEN="${VLLM_MAX_LEN_FAST_IMAGE:-49152}"
            KV_CACHE_DTYPE="${VLLM_KV_CACHE_DTYPE_FAST_IMAGE:-auto}"
            SWAP_SPACE="${VLLM_SWAP_SPACE_FAST_IMAGE:-8}"
            CPU_OFFLOAD_GB="${VLLM_CPU_OFFLOAD_FAST_IMAGE:-0}"
            MAX_NUM_SEQS=4
            MAX_NUM_BATCHED_TOKENS=8192
            ATTENTION_BACKEND="TRITON_ATTN"
            ENFORCE_EAGER="true"
            GPU_MEMORY_UTILIZATION="0.70"
            SERVED_MODEL_NAME="fast"
            ;;
        *)
            echo "Unknown mode: $1" >&2
            usage
            exit 1
            ;;
    esac
}

declare -a VLLM_CMD=()

validate_mode_model() {
    if [[ "$MODE" != "extreme" ]]; then
        return 0
    fi

    # If MODEL is a local filesystem path, validate expected artifacts.
    if [[ "$MODEL" == /* ]]; then
        if [[ ! -d "$MODEL" ]]; then
            echo "Extreme mode model path not found: $MODEL" >&2
            echo "Run convert-to-fp8.py first, or set VLLM_EXTREME_MODEL to another model." >&2
            exit 1
        fi
        if [[ ! -f "$MODEL/config.json" ]]; then
            echo "Missing config.json in extreme mode model path: $MODEL" >&2
            exit 1
        fi

        shopt -s nullglob
        local consolidated_files=("$MODEL"/consolidated*.safetensors)
        local hf_shard_files=("$MODEL"/model-*.safetensors)
        shopt -u nullglob

        if (( ${#consolidated_files[@]} == 0 )) && \
           [[ ! -f "$MODEL/consolidated.safetensors" ]] && \
           (( ${#hf_shard_files[@]} == 0 )) && \
           [[ ! -f "$MODEL/model.safetensors" ]] && \
           [[ ! -f "$MODEL/model.safetensors.index.json" ]]; then
            echo "No recognized weights found in $MODEL" >&2
            echo "Expected one of:" >&2
            echo "  - consolidated*.safetensors (Mistral format)" >&2
            echo "  - model-*.safetensors / model.safetensors*.json (HF format)" >&2
            exit 1
        fi
    fi
}

build_vllm_cmd() {
    VLLM_CMD=(
        "$VLLM_DIR/.venv/bin/vllm" serve "$MODEL"
        --host 0.0.0.0
        --port 11434
        --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
        --max-model-len "$MAX_LEN"
        --dtype auto
    )
    [[ -n "$QUANTIZATION" ]] && VLLM_CMD+=(--quantization "$QUANTIZATION")
    [[ -n "$KV_CACHE_DTYPE" ]] && VLLM_CMD+=(--kv-cache-dtype "$KV_CACHE_DTYPE")
    [[ -n "$SWAP_SPACE" ]] && VLLM_CMD+=(--swap-space "$SWAP_SPACE")
    [[ -n "$CPU_OFFLOAD_GB" ]] && VLLM_CMD+=(--cpu-offload-gb "$CPU_OFFLOAD_GB")
    if [[ -n "$KV_OFFLOADING_SIZE" && "$KV_OFFLOADING_SIZE" != "0" ]]; then
        VLLM_CMD+=(--kv-offloading-size "$KV_OFFLOADING_SIZE")
        [[ -n "$KV_OFFLOADING_BACKEND" ]] && VLLM_CMD+=(--kv-offloading-backend "$KV_OFFLOADING_BACKEND")
    fi
    if [[ "$DISABLE_HYBRID_KV_CACHE_MANAGER" == "true" ]]; then
        VLLM_CMD+=(--disable-hybrid-kv-cache-manager)
    fi
    [[ -n "$MAX_NUM_SEQS" ]] && VLLM_CMD+=(--max-num-seqs "$MAX_NUM_SEQS")
    [[ -n "$MAX_NUM_BATCHED_TOKENS" ]] && VLLM_CMD+=(--max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS")
    [[ -n "$ATTENTION_BACKEND" ]] && VLLM_CMD+=(--attention-backend "$ATTENTION_BACKEND")
    [[ -n "$ENFORCE_EAGER" ]] && VLLM_CMD+=(--enforce-eager)
    if [[ "$CALCULATE_KV_SCALES" == "1" || "$CALCULATE_KV_SCALES" == "true" || "$CALCULATE_KV_SCALES" == "TRUE" ]]; then
        VLLM_CMD+=(--calculate-kv-scales)
    fi
    VLLM_CMD+=(
        --tokenizer_mode "$TOKENIZER_MODE"
        --config_format "$CONFIG_FORMAT"
        --load_format "$LOAD_FORMAT"
        --enable-auto-tool-choice
        --tool-call-parser mistral
        --served-model-name "$SERVED_MODEL_NAME"
    )
    # Chunked prefill and prefix caching are opt-in via env vars.
    # These features can cause quality degradation with certain attention
    # backends (TRITON_ATTN + FP8) on RTX 50-series.
    if [[ "${VLLM_ENABLE_CHUNKED_PREFILL:-0}" == "1" ]]; then
        VLLM_CMD+=(--enable-chunked-prefill)
    fi
    if [[ "${VLLM_ENABLE_PREFIX_CACHING:-0}" == "1" ]]; then
        VLLM_CMD+=(--enable-prefix-caching)
    fi
}

build_mode_profile "$MODE"
validate_mode_model
build_vllm_cmd

if [[ "$PRINT_EXECSTART" == "true" ]]; then
    printf '%q ' "${VLLM_CMD[@]}"
    echo
    exit 0
fi

echo "Starting vLLM server in $MODE mode..."
echo "Model: $MODEL"
echo "Max context length: $MAX_LEN"
echo "Tokenizer mode: $TOKENIZER_MODE"
echo "Config format: $CONFIG_FORMAT"
echo "Load format: $LOAD_FORMAT"
echo "KV spillover RAM per GPU: ${SWAP_SPACE:-0} GiB"
echo "CPU weight offload per GPU: ${CPU_OFFLOAD_GB:-0} GiB"
echo "Log: $LOG_DIR/vllm-$MODE.log"

export HF_HOME="${HF_HOME:-${MODELS_ROOT:-/opt/models}/huggingface}"
export CUDA_VISIBLE_DEVICES="0"
# RTX 50-series on current CUDA stacks can fail FlashInfer JIT (compute_120a).
export VLLM_ATTENTION_BACKEND="${ATTENTION_BACKEND:-FLASH_ATTN}"
if [[ -n "$PYTORCH_CUDA_ALLOC_CONF_VALUE" ]]; then
    export PYTORCH_CUDA_ALLOC_CONF="$PYTORCH_CUDA_ALLOC_CONF_VALUE"
fi

# Activate virtual environment
source "$VLLM_DIR/.venv/bin/activate"

if [[ "$DRY_RUN" == "true" ]]; then
    printf '%q ' "${VLLM_CMD[@]}"
    echo
    exit 0
fi

exec "${VLLM_CMD[@]}" 2>&1 | tee -a "$LOG_DIR/vllm-$MODE.log"

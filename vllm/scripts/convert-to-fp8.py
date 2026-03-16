#!/usr/bin/env python3
"""
Convert Mistral-Small-3.2-24B-Instruct-2506 BF16 weights to FP8.

Important:
- `manual-mistral` (default):
  - Output key names and file names are written in consolidated Mistral format so
    vLLM can load with `--load_format mistral --config_format mistral`.
  - Only language-model projection weights are converted to FP8 + weight scale.
    Vision and connector tensors remain BF16 for compatibility.
- `auto-fp8` / `infermatic`:
  - Uses AutoFP8 dynamic quantization flow from the Infermatic guide.
  - Produces HF-style output (use `--load_format auto --config_format auto`).

Usage:
    /opt/ai/vllm/.venv/bin/python3 convert-to-fp8.py
"""

import argparse
import gc
import json
import shutil
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

DEFAULT_SRC_DIR = Path(
    "/opt/models/huggingface/hub/models--mistralai--Mistral-Small-3.2-24B-Instruct-2506"
)
DEFAULT_OUT_DIR = Path("/opt/models/Mistral-Small-3.2-24B-FP8")


def hf_to_mistral_consolidated(name: str) -> str:
    """Map HF sharded names to consolidated Mistral/Pixtral names."""
    # Language model block
    name = name.replace("language_model.model.", "")
    name = name.replace("language_model.lm_head", "output")
    name = name.replace("embed_tokens", "tok_embeddings")
    name = name.replace("self_attn.q_proj", "attention.wq")
    name = name.replace("self_attn.k_proj", "attention.wk")
    name = name.replace("self_attn.v_proj", "attention.wv")
    name = name.replace("self_attn.o_proj", "attention.wo")
    name = name.replace("mlp.gate_proj", "feed_forward.w1")
    name = name.replace("mlp.down_proj", "feed_forward.w2")
    name = name.replace("mlp.up_proj", "feed_forward.w3")
    name = name.replace("input_layernorm", "attention_norm")
    name = name.replace("post_attention_layernorm", "ffn_norm")
    name = name.replace("model.norm", "norm")

    # Vision tower block
    name = name.replace("vision_tower.", "vision_encoder.")
    name = name.replace("attention.q_proj", "attention.wq")
    name = name.replace("attention.k_proj", "attention.wk")
    name = name.replace("attention.v_proj", "attention.wv")
    name = name.replace("attention.o_proj", "attention.wo")
    name = name.replace("feed_forward.gate_proj", "feed_forward.w1")
    name = name.replace("feed_forward.down_proj", "feed_forward.w2")
    name = name.replace("feed_forward.up_proj", "feed_forward.w3")

    # Multimodal projector / adapter block
    name = name.replace("multi_modal_projector.linear_1", "vision_language_adapter.w_in")
    name = name.replace("multi_modal_projector.linear_2", "vision_language_adapter.w_out")
    name = name.replace("multi_modal_projector.norm", "pre_mm_projector_norm")
    name = name.replace("multi_modal_projector.patch_merger", "patch_merger")

    return name


def is_fp8_weight(name: str) -> bool:
    """
    FP8-quantize only language-model linear projections.
    Everything else (including vision / adapters) remains BF16.
    """
    if not name.endswith(".weight"):
        return False
    if not name.startswith("layers."):
        return False

    parts = name.split(".")
    if len(parts) != 5:
        return False
    if not parts[1].isdigit():
        return False

    block = parts[2]
    proj = parts[3]
    if block == "attention" and proj in {"wq", "wk", "wv", "wo"}:
        return True
    if block == "feed_forward" and proj in {"w1", "w2", "w3"}:
        return True
    return False


def quantize_to_fp8(tensor: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Quantize a BF16 tensor to FP8 E4M3 with per-tensor scale."""
    max_fp8 = torch.finfo(torch.float8_e4m3fn).max  # 448.0
    abs_max = tensor.abs().max().float().clamp(min=1e-12)
    scale = abs_max / max_fp8
    fp8_tensor = (tensor.float() / scale).clamp(-max_fp8, max_fp8).to(torch.float8_e4m3fn)
    return fp8_tensor, scale.to(torch.float32)


def build_weight_map_from_header(path: Path) -> dict[str, str]:
    with path.open("rb") as f:
        header_size = int.from_bytes(f.read(8), "little")
        header = json.loads(f.read(header_size))
    return {k: path.name for k in header if k != "__metadata__"}


def resolve_snapshot(src_dir: Path, snapshot: str | None) -> Path:
    if snapshot is not None:
        snap = src_dir / "snapshots" / snapshot
        if not snap.exists():
            raise FileNotFoundError(f"Snapshot not found: {snap}")
        return snap

    snapshots = sorted((src_dir / "snapshots").glob("*"))
    if not snapshots:
        raise FileNotFoundError(f"No snapshots found under: {src_dir / 'snapshots'}")
    return snapshots[-1]


def clear_previous_outputs(out_dir: Path) -> None:
    for pattern in ("consolidated*.safetensors", "model-*.safetensors", "*.safetensors.index.json"):
        for path in out_dir.glob(pattern):
            path.unlink(missing_ok=True)


def has_recognized_weights(model_dir: Path) -> bool:
    if not model_dir.is_dir():
        return False
    consolidated = list(model_dir.glob("consolidated*.safetensors"))
    hf_shards = list(model_dir.glob("model-*.safetensors"))
    return bool(consolidated) or bool(hf_shards) or (model_dir / "consolidated.safetensors").exists() or (
        model_dir / "model.safetensors"
    ).exists() or (model_dir / "model.safetensors.index.json").exists()


def validate_output(model_dir: Path, method: str) -> None:
    config_path = model_dir / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"Conversion output is missing config.json: {config_path}")
    if not has_recognized_weights(model_dir):
        raise RuntimeError(f"Conversion output has no recognized safetensors weights: {model_dir}")

    if method == "manual-mistral":
        if not list(model_dir.glob("consolidated*.safetensors")) and not (model_dir / "consolidated.safetensors").exists():
            raise RuntimeError(
                "manual-mistral output must contain consolidated*.safetensors "
                f"or consolidated.safetensors: {model_dir}"
            )
    else:
        if not list(model_dir.glob("model-*.safetensors")) and not (model_dir / "model.safetensors").exists():
            raise RuntimeError(
                "auto-fp8/infermatic output must contain model-*.safetensors "
                f"or model.safetensors: {model_dir}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Mistral-Small-3.2-24B weights to FP8.")
    parser.add_argument("--src-dir", type=Path, default=DEFAULT_SRC_DIR)
    parser.add_argument("--src-snapshot", type=str, default=None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument(
        "--method",
        choices=["manual-mistral", "auto-fp8", "infermatic"],
        default="manual-mistral",
        help="Quantization method. auto-fp8/infermatic follows the Infermatic AutoFP8 flow.",
    )
    parser.add_argument(
        "--examples-file",
        type=Path,
        default=None,
        help="Optional JSON file with calibration examples for --method auto-fp8.",
    )
    parser.add_argument(
        "--max-shards",
        type=int,
        default=0,
        help="Debug option: process only the first N shards (0 = all).",
    )
    return parser.parse_args()


def load_examples(path: Path | None) -> list[dict]:
    if path is None:
        return []
    payload = json.loads(path.read_text())
    if not isinstance(payload, list):
        raise ValueError(f"Examples file must be a JSON list: {path}")
    cleaned: list[dict] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"Examples item {idx} must be an object.")
        cleaned.append(item)
    return cleaned


def convert_with_auto_fp8(args: argparse.Namespace, src_snap: Path) -> None:
    try:
        from auto_fp8 import AutoFP8ForCausalLM, BaseQuantizeConfig
    except ImportError as exc:
        raise RuntimeError(
            "auto-fp8 method requires the AutoFP8 package. "
            "Install it in /opt/ai/vllm/.venv first "
            "(for example: /opt/ai/vllm/.venv/bin/pip install auto-fp8)."
        ) from exc

    args.out_dir.mkdir(parents=True, exist_ok=True)
    examples = load_examples(args.examples_file)
    quantize_config = BaseQuantizeConfig(
        quant_method="fp8",
        activation_scheme="dynamic",
    )

    print(f"Source snapshot: {src_snap}")
    print(f"Output: {args.out_dir}")
    print(f"Method: auto-fp8 (dynamic)")
    print(f"Examples: {len(examples)}")

    model = AutoFP8ForCausalLM.from_pretrained(str(src_snap), quantize_config)
    model.quantize(examples)
    model.save_quantized(str(args.out_dir))

    # Keep auxiliary Mistral files for downstream launch scripts.
    for fname in (
        "params.json",
        "tekken.json",
        "generation_config.json",
        "SYSTEM_PROMPT.txt",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "tokenizer.model",
        "processor_config.json",
        "preprocessor_config.json",
        "chat_template.jinja",
    ):
        src = src_snap / fname
        if src.exists():
            shutil.copy2(str(src), str(args.out_dir / fname))
            print(f"Copied {fname}")

    print(f"\nDone! AutoFP8 model saved to {args.out_dir}")
    print("\nLaunch with:")
    print(f"  vllm serve {args.out_dir} --quantization fp8 --load_format auto \\")
    print("    --config_format auto")


def convert_manual_mistral(args: argparse.Namespace, src_snap: Path) -> None:

    args.out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous_outputs(args.out_dir)

    shards = sorted(src_snap.glob("model-*.safetensors"))
    if not shards:
        raise FileNotFoundError(f"No model shards found in {src_snap}")
    if args.max_shards > 0:
        shards = shards[: args.max_shards]

    print(f"Source snapshot: {src_snap}")
    print(f"Found {len(shards)} shard(s)")
    print(f"Output: {args.out_dir}")
    print(f"Device: {args.device}")

    shard_files: list[str] = []
    total_params = 0
    total_fp8_params = 0
    total_bf16_params = 0

    for shard_idx, shard_path in enumerate(shards):
        print(f"\n[{shard_idx + 1}/{len(shards)}] Processing {shard_path.name}...")
        shard = load_file(str(shard_path), device=args.device)
        out_tensors: dict[str, torch.Tensor] = {}

        for hf_name, tensor in shard.items():
            out_name = hf_to_mistral_consolidated(hf_name)
            total_params += tensor.numel()

            if is_fp8_weight(out_name):
                fp8_tensor, scale = quantize_to_fp8(tensor)
                out_tensors[out_name] = fp8_tensor.cpu()
                out_tensors[f"{out_name}_scale"] = scale.cpu()
                total_fp8_params += tensor.numel()
                print(f"  {out_name}: FP8 {tuple(tensor.shape)} (scale={scale.item():.6f})")
            else:
                out_tensors[out_name] = tensor.cpu().to(torch.bfloat16)
                total_bf16_params += tensor.numel()
                print(f"  {out_name}: BF16 {tuple(tensor.shape)}")

        # `consolidated*` naming is required by vLLM `--load_format mistral`.
        out_file = f"consolidated-{shard_idx + 1:05d}-of-{len(shards):05d}.safetensors"
        out_path = args.out_dir / out_file
        save_file(out_tensors, str(out_path))
        shard_files.append(out_file)
        print(f"  Saved {out_file} ({out_path.stat().st_size / 1e9:.2f} GB)")

        del shard, out_tensors
        gc.collect()
        if args.device.startswith("cuda"):
            torch.cuda.empty_cache()

    print(f"\nTotal parameters: {total_params:,}")
    print(f"FP8 parameters:   {total_fp8_params:,}")
    print(f"BF16 parameters:  {total_bf16_params:,}")

    # Write consolidated index
    weight_map: dict[str, str] = {}
    for out_name in shard_files:
        weight_map.update(build_weight_map_from_header(args.out_dir / out_name))

    index = {
        "metadata": {"total_size": sum((args.out_dir / f).stat().st_size for f in shard_files)},
        "weight_map": weight_map,
    }
    (args.out_dir / "consolidated.safetensors.index.json").write_text(json.dumps(index, indent=2))
    print("Wrote consolidated.safetensors.index.json")

    # Copy config with explicit FP8 quantization info.
    config = json.loads((src_snap / "config.json").read_text())
    config.pop("quantization_config", None)
    config["quantization_config"] = {
        "quant_method": "fp8",
        "activation_scheme": "dynamic",
        "fmt": "e4m3",
    }
    (args.out_dir / "config.json").write_text(json.dumps(config, indent=2))
    print("Wrote config.json")

    for fname in [
        "params.json",
        "tekken.json",
        "generation_config.json",
        "SYSTEM_PROMPT.txt",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "tokenizer.model",
        "processor_config.json",
        "preprocessor_config.json",
        "chat_template.jinja",
    ]:
        src = src_snap / fname
        if src.exists():
            shutil.copy2(str(src), str(args.out_dir / fname))
            print(f"Copied {fname}")

    print(f"\nDone! Model saved to {args.out_dir}")
    print("\nLaunch with:")
    print(f"  vllm serve {args.out_dir} --quantization fp8 --load_format mistral \\")
    print("    --config_format mistral --tokenizer_mode mistral")


def main():
    args = parse_args()
    if args.device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("Requested --device cuda but CUDA is not available.")
    src_snap = resolve_snapshot(args.src_dir, args.src_snapshot)
    selected_method = "auto-fp8" if args.method in {"auto-fp8", "infermatic"} else "manual-mistral"
    if selected_method == "auto-fp8":
        convert_with_auto_fp8(args, src_snap)
    else:
        convert_manual_mistral(args, src_snap)
    validate_output(args.out_dir, selected_method)
    print(f"Validated conversion output: {args.out_dir}")


if __name__ == "__main__":
    main()

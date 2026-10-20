"""Merge a trained adapter into a CPU Hugging Face checkpoint for GGUF conversion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from llm_training.train import check_output, sha256_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args(argv)
    check_output(args.output, args.adapter)
    metadata_file = args.adapter / "run_metadata.json"
    config_file = args.adapter / "adapter_config.json"
    if not metadata_file.is_file() or not config_file.is_file():
        raise ValueError("Adapter requires run_metadata.json and adapter_config.json")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    config = json.loads(config_file.read_text(encoding="utf-8"))
    model_id = metadata.get("base_model")
    if not model_id or config.get("base_model_name_or_path") != model_id:
        raise ValueError("Adapter base model differs from run metadata")
    model_path = Path(model_id)
    if model_path.exists():
        check_output(args.output, model_path)
    if not (args.adapter / "adapter_model.safetensors").is_file():
        raise ValueError("Missing safe adapter weights")
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("Install pinned LLM dependencies before export") from exc
    # Float16 CPU merge keeps a 4B checkpoint practical on a 32 GB RAM host.
    base = AutoModelForCausalLM.from_pretrained(
        model_id, revision=metadata.get("base_revision"), local_files_only=args.local_files_only,
        trust_remote_code=False, torch_dtype=torch.float16, low_cpu_mem_usage=True,
        device_map="cpu", attn_implementation="sdpa")
    model = PeftModel.from_pretrained(base, args.adapter, is_trainable=False, local_files_only=True)
    merged = model.merge_and_unload(safe_merge=True)
    tokenizer = AutoTokenizer.from_pretrained(args.adapter, local_files_only=True, trust_remote_code=False)
    args.output.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(args.output, safe_serialization=True, max_shard_size="2GB")
    tokenizer.save_pretrained(args.output)
    export_info = {"base_model": model_id, "base_revision": metadata.get("base_revision"),
                   "adapter_sha256": sha256_file(args.adapter / "adapter_model.safetensors"),
                   "run_metadata_sha256": sha256_file(metadata_file), "format": "Hugging Face safetensors float16",
                   "next_step": "Convert with llama.cpp convert_hf_to_gguf.py, then quantize to GGUF for Ollama."}
    (args.output / "export_metadata.json").write_text(json.dumps(export_info, indent=2) + "\n", encoding="utf-8")
    # Ollama reads the GGUF produced by the separate llama.cpp conversion step.
    (args.output / "Modelfile.gguf.template").write_text(
        "FROM ./watcher-eve-Q4_K_M.gguf\nPARAMETER num_ctx 1024\n", encoding="utf-8")
    print(json.dumps({"merged_checkpoint": str(args.output),
                      "modelfile_template": str(args.output / "Modelfile.gguf.template")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Completion-only QLoRA training for a 12 GB NVIDIA GPU."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from llm_training.data import validate_record
from llm_training.tokenization import supervised_tokens

DEFAULT_MODEL = "Qwen/Qwen3-4B"
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_split(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"Missing prepared split: {path}")
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_number}: blank row")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise ValueError(f"{path}:{line_number}: expected a nonempty string id")
            if not isinstance(row.get("group"), str) or not row["group"]:
                raise ValueError(f"{path}:{line_number}: expected a nonempty string group")
            messages = row.get("messages")
            if not isinstance(messages, list) or len(messages) < 2:
                raise ValueError(f"{path}:{line_number}: expected at least two messages")
            for message in messages:
                if (not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}
                        or not isinstance(message.get("content"), str)):
                    raise ValueError(f"{path}:{line_number}: malformed message")
            if messages[-1]["role"] != "assistant" or not messages[-1]["content"]:
                raise ValueError(f"{path}:{line_number}: final message must be a nonempty assistant target")
            try:
                if validate_record(row) != row:
                    raise ValueError("prepared row is not normalized")
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            rows.append(row)
    if not rows:
        raise ValueError(f"Empty prepared split: {path}")
    return rows


def load_data(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    train_path, eval_path = directory / "train.jsonl", directory / "eval.jsonl"
    train, heldout = load_split(train_path), load_split(eval_path)
    train_ids = {row["id"] for row in train}
    eval_ids = {row["id"] for row in heldout}
    if len(train_ids) != len(train) or len(eval_ids) != len(heldout) or train_ids & eval_ids:
        raise ValueError("Example IDs must be unique across train and eval")
    train_groups = {row["group"] for row in train}
    if train_groups & {row["group"] for row in heldout}:
        raise ValueError("Train and eval share a group; regenerate the prepared split")
    hashes = {"train.jsonl": sha256_file(train_path), "eval.jsonl": sha256_file(eval_path)}
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        raise ValueError(f"Missing prepared manifest: {manifest}")
    config = json.loads(manifest.read_text(encoding="utf-8"))
    if config.get("format_version") != 1 or config.get("chat_template") != {
        "enable_thinking": False, "add_generation_prompt": True,
        "sequence_format": "generation_prompt + encoded final assistant content + eos"}:
        raise ValueError("Unsupported prepared dataset format or chat template")
    for split, rows in (("train", train), ("eval", heldout)):
        expected = config.get("splits", {}).get(split, {})
        if expected.get("sha256") != hashes[f"{split}.jsonl"] or expected.get("records") != len(rows):
            raise ValueError(f"Manifest {split} hash or record count differs from JSONL")
        if expected.get("groups") != sorted({row["group"] for row in rows}):
            raise ValueError(f"Manifest {split} groups differ from JSONL")
        if expected.get("tokens") != sum(config.get("record_tokens", {}).get(row["id"], -1) for row in rows):
            raise ValueError(f"Manifest {split} token total differs from record counts")
    if set(config.get("record_tokens", {})) != train_ids | eval_ids:
        raise ValueError("Manifest token counts do not match example IDs")
    if not isinstance(config.get("max_length"), int) or config["max_length"] < 1:
        raise ValueError("Manifest max_length must be positive")
    if any(type(value) is not int or value < 1 or value > config["max_length"]
           for value in config["record_tokens"].values()):
        raise ValueError("Manifest has invalid or oversized record token counts")
    if {row["messages"][1]["content"].strip().casefold() for row in train} & {row["messages"][1]["content"].strip().casefold() for row in heldout}:
        raise ValueError("Identical initial prompts appear in train and eval")
    hashes["manifest.json"] = sha256_file(manifest)
    return train, heldout, hashes


def check_output(output: Path, *inputs: Path) -> None:
    resolved = output.resolve()
    for path in inputs:
        source = path.resolve()
        if resolved == source or source in resolved.parents or resolved in source.parents:
            raise ValueError(f"Output {output} overlaps input {path}")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output must be a new or empty directory: {output}")


def tokenize_example(tokenizer: Any, messages: list[dict[str, str]], max_length: int) -> dict[str, list[int]]:
    result = supervised_tokens(tokenizer, messages)
    if len(result["input_ids"]) > max_length:
        raise ValueError(f"Example has {len(result['input_ids'])} tokens, over --max-length {max_length}; no truncation performed")
    return result


class Examples:
    def __init__(self, rows: list[dict[str, Any]], tokenizer: Any, max_length: int):
        self.items = []
        for row in rows:
            try:
                self.items.append(tokenize_example(tokenizer, row["messages"], max_length))
            except ValueError as exc:
                raise ValueError(f"Example {row['id']}: {exc}") from exc

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        return self.items[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--revision", help="Optional pinned Hugging Face model revision")
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.max_length < 32 or args.max_steps is not None and args.max_steps < 1:
        parser.error("--max-length must be at least 32 and --max-steps must be positive")
    train_rows, eval_rows, hashes = load_data(args.data)
    check_output(args.output, args.data)
    model_path = Path(args.model)
    if model_path.exists():
        check_output(args.output, model_path)
    manifest = json.loads((args.data / "manifest.json").read_text(encoding="utf-8"))
    if args.model != manifest.get("tokenizer") or args.max_length > manifest.get("max_length", 0):
        raise ValueError("Model must match prepared tokenizer and max length cannot exceed prepared limit")
    if any(count > args.max_length for count in manifest["record_tokens"].values()):
        raise ValueError("Prepared examples exceed --max-length; no truncation performed")
    if args.dry_run:
        print(json.dumps({"train_examples": len(train_rows), "eval_examples": len(eval_rows),
                          "data_sha256": hashes, "model": args.model, "max_length": args.max_length}, indent=2))
        return 0

    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, Trainer, TrainingArguments
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    except ImportError as exc:
        raise RuntimeError("Install the pinned LLM training dependencies before training") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("QLoRA training requires an NVIDIA CUDA GPU; --dry-run validates data without one")
    gpu = torch.cuda.get_device_properties(0)
    if gpu.total_memory < 10 * 1024**3:
        raise RuntimeError(f"GPU {gpu.name} has {gpu.total_memory / 1024**3:.1f} GiB; this recipe needs about 12 GiB")
    print(f"Training on {gpu.name}, {gpu.total_memory / 1024**3:.1f} GiB VRAM")
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision,
                                              local_files_only=args.local_files_only, trust_remote_code=False)
    if sha256_file_text(str(tokenizer.chat_template)) != manifest.get("chat_template_sha256"):
        raise ValueError("Loaded tokenizer chat template differs from prepared dataset")
    tokenizer_revision = getattr(tokenizer, "init_kwargs", {}).get("_commit_hash")
    if manifest.get("tokenizer_revision") not in ("unknown", tokenizer_revision):
        raise ValueError("Loaded tokenizer revision differs from prepared dataset")
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    train_set, eval_set = (Examples(rows, tokenizer, args.max_length) for rows in (train_rows, eval_rows))
    for split_name, rows, dataset in (("train", train_rows, train_set), ("eval", eval_rows, eval_set)):
        for row, item in zip(rows, dataset.items):
            if len(item["input_ids"]) != manifest["record_tokens"][row["id"]]:
                raise ValueError(f"Prepared token count differs for {split_name} example {row['id']}")
    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                              bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.float16)
    try:
        model = AutoModelForCausalLM.from_pretrained(
            args.model, revision=args.revision, local_files_only=args.local_files_only,
            trust_remote_code=False, quantization_config=quant, device_map={"": 0}, torch_dtype=torch.float16,
            attn_implementation="sdpa")
    except torch.cuda.OutOfMemoryError as exc:
        raise RuntimeError("Model load exhausted GPU memory; close GPU processes or use the 4-bit recipe on a 12 GB GPU") from exc
    base_revision = getattr(model.config, "_commit_hash", None) or args.revision
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05,
                                              target_modules=TARGET_MODULES, bias="none", task_type="CAUSAL_LM"))
    args.output.mkdir(parents=True, exist_ok=True)
    training_args = TrainingArguments(
        output_dir=str(args.output), per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=16, gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False}, fp16=True, bf16=False,
        learning_rate=2e-4, lr_scheduler_type="cosine", warmup_ratio=0.03,
        num_train_epochs=3, max_steps=args.max_steps or -1, optim="paged_adamw_8bit",
        logging_steps=10, save_strategy="no", eval_strategy="no", report_to="none",
        remove_unused_columns=False, dataloader_num_workers=0)
    def collate(items: list[dict[str, list[int]]]) -> dict[str, Any]:
        return {key: torch.nn.utils.rnn.pad_sequence(
            [torch.tensor(item[key], dtype=torch.long) for item in items], batch_first=True,
            padding_value=(-100 if key == "labels" else tokenizer.pad_token_id if key == "input_ids" else 0))
            for key in ("input_ids", "attention_mask", "labels")}
    trainer = Trainer(model=model, args=training_args, train_dataset=train_set,
                      eval_dataset=eval_set, data_collator=collate)
    try:
        trainer.train()
        heldout = trainer.evaluate()
    except torch.cuda.OutOfMemoryError as exc:
        raise RuntimeError("Training exhausted GPU memory; lower --max-length or free GPU memory") from exc
    model.save_pretrained(args.output, safe_serialization=True)
    tokenizer.save_pretrained(args.output)
    metadata = {"base_model": args.model, "base_revision": base_revision,
                "data_sha256": hashes, "train_examples": len(train_rows), "eval_examples": len(eval_rows),
                "max_length": args.max_length, "heldout_loss": heldout.get("eval_loss"),
                "training": {"epochs": 3, "max_steps": args.max_steps, "batch_size": 1,
                             "gradient_accumulation_steps": 16, "lora_rank": 8,
                             "target_modules": TARGET_MODULES, "quantization": "NF4 double quantization FP16"}}
    (args.output / "run_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"adapter": str(args.output), "heldout_loss": metadata["heldout_loss"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

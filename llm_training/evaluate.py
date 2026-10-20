"""Run deterministic held-out generation and measure action-format agreement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from llm_training.train import DEFAULT_MODEL, load_data, sha256_file
from llm_training.protocol import ProtocolError, parse_action
from llm_training.tokenization import generation_prompt_ids


def score_prediction(target: str, prediction: str) -> dict[str, Any]:
    """Compare protocol actions; this does not certify factual correctness."""
    try:
        expected = parse_action(target)
    except ProtocolError as exc:
        raise ValueError(f"Invalid prepared target: {exc}") from exc
    try:
        actual = parse_action(prediction)
    except ProtocolError:
        return {"valid": False, "action_exact": False, "tool_exact": False,
                "answer_exact": False, "answer_present": False}
    expected_tool = expected.get("tool")
    actual_tool = actual.get("tool")
    is_answer = "answer" in expected
    return {"valid": True, "action_exact": actual == expected,
            "tool_exact": expected_tool is not None and actual_tool == expected_tool
                          and actual.get("arguments") == expected.get("arguments"),
            "answer_exact": is_answer and actual.get("answer") == expected.get("answer"),
            "answer_present": is_answer and isinstance(actual.get("answer"), str)
                              and bool(actual["answer"].strip())}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    targets = [r for r in rows if r["target_kind"] == "answer"]
    tools = [r for r in rows if r["target_kind"] == "tool"]
    def rate(items: list[dict[str, Any]], key: str) -> float | None:
        return sum(bool(row["scores"][key]) for row in items) / len(items) if items else None
    return {"examples": count, "valid_protocol_rate": rate(rows, "valid"),
            "action_exact_rate": rate(rows, "action_exact"),
            "tool_and_args_exact_rate": rate(tools, "tool_exact"), "tool_targets": len(tools),
            "answer_exact_rate": rate(targets, "answer_exact"),
            "answer_presence_rate": rate(targets, "answer_present"), "answer_targets": len(targets)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-model", default=None, help="Override metadata base model (must match adapter config)")
    parser.add_argument("--baseline", action="store_true", help="Also evaluate the untuned base model")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args(argv)
    if args.max_new_tokens < 1:
        parser.error("--max-new-tokens must be positive")
    _, heldout, hashes = load_data(args.data)
    metadata_path = args.adapter / "run_metadata.json"
    config_path = args.adapter / "adapter_config.json"
    if not metadata_path.is_file() or not config_path.is_file():
        raise ValueError("Adapter needs run_metadata.json and adapter_config.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    model_id = args.base_model or metadata.get("base_model") or DEFAULT_MODEL
    if model_id != config.get("base_model_name_or_path") or model_id != metadata.get("base_model"):
        raise ValueError("Base model differs between CLI, adapter config, and run metadata")
    if hashes != metadata.get("data_sha256"):
        raise ValueError("Prepared train, eval, or manifest hash differs from the adapter's training run")
    output = args.output.resolve()
    for source in (args.adapter.resolve(), args.data.resolve()):
        if output == source or source in output.parents or output in source.parents:
            raise ValueError("Report output overlaps adapter or data directory")
    if output.exists():
        raise ValueError(f"Refusing to overwrite report: {output}")
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    except ImportError as exc:
        raise RuntimeError("Install pinned LLM dependencies before evaluation") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("4-bit evaluation requires an NVIDIA CUDA GPU")
    revision = metadata.get("base_revision")
    tokenizer = AutoTokenizer.from_pretrained(args.adapter, local_files_only=True, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    def load_model() -> Any:
        base = AutoModelForCausalLM.from_pretrained(
            model_id, revision=revision, local_files_only=args.local_files_only, trust_remote_code=False,
            quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.float16),
            device_map={"": 0}, torch_dtype=torch.float16, attn_implementation="sdpa")
        base.eval()
        return base

    def generate(model: Any) -> list[dict[str, Any]]:
        outcomes = []
        for row in heldout:
            prompt_ids = generation_prompt_ids(tokenizer, row["messages"][:-1])
            inputs = {"input_ids": torch.tensor([prompt_ids], device="cuda:0"),
                      "attention_mask": torch.ones((1, len(prompt_ids)), dtype=torch.long, device="cuda:0")}
            if inputs["input_ids"].shape[1] > metadata["max_length"]:
                raise ValueError(f"Example {row['id']} prompt exceeds trained max length")
            with torch.inference_mode():
                generated = model.generate(**inputs, max_new_tokens=args.max_new_tokens,
                                           do_sample=False, pad_token_id=tokenizer.pad_token_id)
            prediction = tokenizer.decode(generated[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
            target = row["messages"][-1]["content"]
            expected = parse_action(target)
            outcomes.append({"id": row["id"], "group": row["group"],
                             "target_kind": "answer" if "answer" in expected else "tool",
                             "target": target, "prediction": prediction,
                             "scores": score_prediction(target, prediction)})
        return outcomes

    base = load_model()
    if args.baseline:
        baseline_rows = generate(base)
    else:
        baseline_rows = None
    model = PeftModel.from_pretrained(base, args.adapter, is_trainable=False, local_files_only=True)
    model.eval()
    adapter_rows = generate(model)
    report = {"adapter": str(args.adapter.resolve()), "base_model": model_id,
              "base_revision": revision, "eval_sha256": sha256_file(args.data / "eval.jsonl"),
              "metrics_scope": "Exact protocol structure, tool and arguments, and answer text only; no factual correctness certification.",
              "adapter_metrics": summarize(adapter_rows), "examples": adapter_rows}
    if baseline_rows is not None:
        report["base_metrics"] = summarize(baseline_rows)
        report["base_examples"] = baseline_rows
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "adapter_metrics": report["adapter_metrics"],
                      "base_metrics": report.get("base_metrics")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

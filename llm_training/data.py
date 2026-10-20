"""Validate and split curated chat examples for local Qwen3-4B fine-tuning.

Input is JSONL with id, group, and messages. Assistant content is always an
action from :mod:`llm_training.protocol`; the last message is the target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .protocol import SYSTEM_PROMPT, ProtocolError, parse_action
from .tokenization import supervised_tokens


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024
MAX_RECORDS = 10_000
MAX_MESSAGES = 20
MAX_CONTENT_BYTES = 16_384


def _canonical(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_label(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{label} must be a nonempty string of at most 128 characters")
    return value


def validate_record(record: dict) -> dict:
    """Return a normalized record, inserting the shared system prompt if absent."""
    if not isinstance(record, dict) or set(record) != {"id", "group", "messages"}:
        raise ValueError("record must contain exactly id, group, messages")
    example_id = _valid_label(record["id"], "id")
    group = _valid_label(record["group"], "group")
    messages = record["messages"]
    if not isinstance(messages, list) or not 2 <= len(messages) <= MAX_MESSAGES:
        raise ValueError(f"{example_id}: messages must contain 2..{MAX_MESSAGES} entries")
    normalized = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError(f"{example_id}: message {index} must contain role and content")
        role, content = message["role"], message["content"]
        if not isinstance(role, str) or role not in {"system", "user", "assistant"}:
            raise ValueError(f"{example_id}: invalid role at message {index}")
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError(f"{example_id}: invalid or oversized content at message {index}")
        if role == "system" and index != 0:
            raise ValueError(f"{example_id}: system message must be first")
        if role == "assistant":
            try:
                parse_action(content)
            except ProtocolError as exc:
                raise ValueError(f"{example_id}: invalid assistant action at message {index}: {exc}") from exc
        if role == "user" and content.lstrip().startswith("{"):
            try:
                user_payload = json.loads(content)
            except ValueError as exc:
                raise ValueError(f"{example_id}: malformed user JSON at message {index}") from exc
            if not isinstance(user_payload, dict) or set(user_payload) != {"tool_result"}:
                raise ValueError(f"{example_id}: user JSON must contain only tool_result")
        normalized.append({"role": role, "content": content})
    if normalized[0]["role"] != "system":
        normalized.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
    elif normalized[0]["content"] != SYSTEM_PROMPT:
        raise ValueError(f"{example_id}: system message must use shared SYSTEM_PROMPT")
    if normalized[1]["role"] != "user" or normalized[-1]["role"] != "assistant":
        raise ValueError(f"{example_id}: first interaction must be user and final target assistant")
    if normalized[1]["content"].lstrip().startswith("{"):
        raise ValueError(f"{example_id}: first user message must be a question, not a tool result")
    for previous, current in zip(normalized[1:], normalized[2:]):
        if previous["role"] == current["role"]:
            raise ValueError(f"{example_id}: repeated adjacent role")
        if previous["role"] == "assistant":
            action = parse_action(previous["content"])
            if "tool" not in action or current["role"] != "user":
                raise ValueError(f"{example_id}: only tool calls may precede another user message")
            if not current["content"].lstrip().startswith("{"):
                raise ValueError(f"{example_id}: tool call requires a JSON tool_result")
    if len(normalized) > MAX_MESSAGES:
        raise ValueError(f"{example_id}: too many messages after system prompt")
    return {"id": example_id, "group": group, "messages": normalized}


def read_records(path: str | Path) -> tuple[list[dict], str]:
    """Read a bounded UTF-8 JSONL file and return records and source SHA-256."""
    path = Path(path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 16 MiB limit")
    raw = path.read_bytes()
    records = []
    ids = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if len(line) > MAX_LINE_BYTES:
            raise ValueError(f"line {line_number} exceeds 64 KiB limit")
        if not line.strip():
            continue
        if len(records) >= MAX_RECORDS:
            raise ValueError("input exceeds 10,000 records")
        try:
            record = validate_record(json.loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"line {line_number}: {exc}") from exc
        if record["id"] in ids:
            raise ValueError(f"duplicate id: {record['id']}")
        ids.add(record["id"])
        records.append(record)
    if len(records) < 2:
        raise ValueError("at least two records are required")
    return records, _digest(raw)


def expand_examples(records: list[dict]) -> list[dict]:
    """Create one supervised prefix for each assistant turn in a conversation.

    The complete conversation retains its source ID. Earlier tool-call targets
    get stable derived IDs and the same group, so holdout never splits turns of
    one scenario across train and evaluation.
    """
    expanded = []
    ids = {record["id"] for record in records}
    if len(ids) != len(records):
        raise ValueError("source example IDs must be unique")
    for source in records:
        record = validate_record(source)
        assistant_indexes = [index for index, message in enumerate(record["messages"])
                             if message["role"] == "assistant"]
        for turn, index in enumerate(assistant_indexes[:-1], 1):
            derived_id = f"turn_{hashlib.sha256(record['id'].encode('utf-8')).hexdigest()[:32]}_{turn}"
            if derived_id in ids:
                raise ValueError(f"derived assistant-turn ID collides with source ID: {derived_id}")
            ids.add(derived_id)
            expanded.append(validate_record({"id": derived_id, "group": record["group"],
                                             "messages": record["messages"][:index + 1]}))
        expanded.append(record)
        if len(expanded) > MAX_RECORDS:
            raise ValueError("expanded input exceeds 10,000 supervised records")
    return expanded


def _prompt_key(record: dict) -> str:
    """A first user question identifies the scenario regardless of later tool output."""
    return record["messages"][1]["content"].strip().casefold()


def split_records(records: list[dict], eval_fraction: float = 0.2) -> tuple[list[dict], list[dict]]:
    """Deterministic group holdout; identical initial prompts cannot cross groups."""
    if not 0 < eval_fraction < 1:
        raise ValueError("eval_fraction must be between 0 and 1")
    normalized = [validate_record(record) for record in records]
    groups = {}
    prompts = {}
    ids = set()
    for record in normalized:
        if record["id"] in ids:
            raise ValueError(f"duplicate id: {record['id']}")
        ids.add(record["id"])
        key = _prompt_key(record)
        previous_group = prompts.setdefault(key, record["group"])
        if previous_group != record["group"]:
            raise ValueError("identical initial prompts appear in different groups")
        groups.setdefault(record["group"], []).append(record)
    if len(groups) < 2:
        raise ValueError("at least two scenario groups are required")
    ordered = sorted(groups, key=lambda group: hashlib.sha256(group.encode("utf-8")).digest())
    eval_count = max(1, min(len(groups) - 1, round(len(groups) * eval_fraction)))
    eval_groups = set(ordered[:eval_count])
    train = sorted((item for group in groups if group not in eval_groups for item in groups[group]), key=lambda r: r["id"])
    evaluation = sorted((item for group in eval_groups for item in groups[group]), key=lambda r: r["id"])
    return train, evaluation


def token_count(tokenizer, messages: list[dict]) -> int:
    """Count the exact supervised token sequence used by training."""
    return len(supervised_tokens(tokenizer, messages)["input_ids"])


def _jsonl_bytes(records: list[dict]) -> bytes:
    return ("".join(_canonical(record) + "\n" for record in records)).encode("utf-8")


def prepare_dataset(input_path: str | Path, output_dir: str | Path, tokenizer, *,
                    tokenizer_name: str = "Qwen/Qwen3-4B", max_length: int = 1024,
                    eval_fraction: float = 0.2) -> dict:
    """Validate, split, count tokens, and atomically publish prepared JSONL files."""
    if type(max_length) is not int or max_length < 1:
        raise ValueError("max_length must be a positive integer")
    records, input_hash = read_records(input_path)
    expanded = expand_examples(records)
    train, evaluation = split_records(expanded, eval_fraction)
    counts = {}
    for record in train + evaluation:
        count = token_count(tokenizer, record["messages"])
        if count > max_length:
            raise ValueError(f"{record['id']}: {count} tokens exceeds max_length {max_length}; no truncation is applied")
        counts[record["id"]] = count
    payloads = {"train.jsonl": _jsonl_bytes(train), "eval.jsonl": _jsonl_bytes(evaluation)}
    manifest = {
        "format_version": 1,
        "tokenizer": tokenizer_name,
        "tokenizer_revision": getattr(tokenizer, "init_kwargs", {}).get("_commit_hash") or "unknown",
        "chat_template_sha256": _digest(str(getattr(tokenizer, "chat_template", "")).encode("utf-8")),
        "chat_template": {"enable_thinking": False, "add_generation_prompt": True,
                          "sequence_format": "generation_prompt + encoded final assistant content + eos"},
        "max_length": max_length,
        "input_sha256": input_hash,
        "source_records": len(records),
        "expanded_records": len(expanded),
        "splits": {
            "train": {"records": len(train), "groups": sorted({r["group"] for r in train}), "sha256": _digest(payloads["train.jsonl"]), "tokens": sum(counts[r["id"]] for r in train)},
            "eval": {"records": len(evaluation), "groups": sorted({r["group"] for r in evaluation}), "sha256": _digest(payloads["eval.jsonl"]), "tokens": sum(counts[r["id"]] for r in evaluation)},
        },
        "record_tokens": counts,
        "limitations": "Curated fictional starter examples; not a production-quality training corpus.",
    }
    payloads["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    output = Path(output_dir)
    if Path(input_path).resolve().is_relative_to(output.resolve()):
        raise ValueError("input may not be inside output directory")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output directory must be absent or empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}-", dir=output.parent) as temporary:
        staging = Path(temporary)
        for name, payload in payloads.items():
            (staging / name).write_bytes(payload)
        os.replace(staging, output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--tokenizer", default="Qwen/Qwen3-4B")
    parser.add_argument("--local-files-only", action="store_true", help="Use only locally cached tokenizer files")
    args = parser.parse_args(argv)
    from transformers import AutoTokenizer  # Lazy: validation tests need no ML dependencies.
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=False, local_files_only=args.local_files_only)
    manifest = prepare_dataset(args.input, args.output, tokenizer, tokenizer_name=args.tokenizer, max_length=args.max_length)
    print(json.dumps({"train": manifest["splits"]["train"]["records"], "eval": manifest["splits"]["eval"]["records"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

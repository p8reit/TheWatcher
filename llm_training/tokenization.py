"""Tokenize a final assistant action exactly as a generated completion."""

from __future__ import annotations

from typing import Any


def _ids(value: Any) -> list[int]:
    if isinstance(value, dict):
        value = value["input_ids"]
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        value = value[0]
    if not isinstance(value, list) or not all(type(token) is int for token in value):
        raise ValueError("Tokenizer did not return one sequence of token IDs")
    return value


def generation_prompt_ids(tokenizer: Any, history: list[dict[str, str]]) -> list[int]:
    """Render and tokenize the non-thinking assistant generation prompt."""
    prompt = _ids(tokenizer.apply_chat_template(
        history, tokenize=True, add_generation_prompt=True, enable_thinking=False))
    if not prompt:
        raise ValueError("Tokenizer returned an empty generation prompt")
    return prompt


def supervised_tokens(tokenizer: Any, messages: list[dict[str, str]]) -> dict[str, list[int]]:
    """Return prompt + final content + end-of-turn ID with completion-only labels.

    Qwen's generation prompt can include a non-thinking marker absent from a
    completed assistant message. Training and inference therefore share the
    actual generation prompt, without guessing a boundary in a full render.
    """
    if not messages or messages[-1].get("role") != "assistant":
        raise ValueError("Final message must be an assistant target")
    prompt = generation_prompt_ids(tokenizer, messages[:-1])
    content = messages[-1].get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("Assistant target must be nonempty text")
    target = _ids(tokenizer(content, add_special_tokens=False)["input_ids"])
    eos = tokenizer.eos_token_id
    if not isinstance(eos, int):
        raise ValueError("Tokenizer has no end-of-turn token ID")
    if not target:
        raise ValueError("Tokenizer returned empty assistant target")
    input_ids = prompt + target + [eos]
    return {"input_ids": input_ids, "attention_mask": [1] * len(input_ids),
            "labels": [-100] * len(prompt) + target + [eos]}

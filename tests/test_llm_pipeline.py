import hashlib
import json
from pathlib import Path

import pytest

from llm_training.evaluate import score_prediction, summarize
from llm_training.export import main as export_main
from llm_training.protocol import SYSTEM_PROMPT
from llm_training.train import check_output, load_data, main as train_main, tokenize_example


def _row(example_id, group, question):
    return {"id": example_id, "group": group, "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
        {"role": "assistant", "content": '{"answer":"Unknown from local observations."}'},
    ]}


def _prepared(tmp_path):
    data = tmp_path / "prepared"
    data.mkdir()
    rows = {"train": [_row("one", "a", "Question one?")],
            "eval": [_row("two", "b", "Question two?")]}
    split_info = {}
    for split, values in rows.items():
        path = data / f"{split}.jsonl"
        path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
        split_info[split] = {"records": 1, "groups": [values[0]["group"]],
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "tokens": 10}
    manifest = {"format_version": 1, "tokenizer": "Qwen/Qwen3-4B", "max_length": 1024,
                "chat_template": {"enable_thinking": False, "add_generation_prompt": True,
                                  "sequence_format": "generation_prompt + encoded final assistant content + eos"},
                "record_tokens": {"one": 10, "two": 10}, "splits": split_info}
    (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return data


def test_dry_run_validates_prepared_data_without_gpu(tmp_path, capsys):
    data = _prepared(tmp_path)
    assert train_main(["--data", str(data), "--output", str(tmp_path / "adapter"), "--dry-run"]) == 0
    assert '"train_examples": 1' in capsys.readouterr().out
    assert not (tmp_path / "adapter").exists()


def test_corrupted_split_and_manifest_are_rejected(tmp_path):
    data = _prepared(tmp_path)
    with (data / "eval.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="blank row"):
        load_data(data)
    (data / "eval.jsonl").write_text(json.dumps(_row("two", "b", "Question two?")) + "\n", encoding="utf-8")
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    manifest["splits"]["eval"]["sha256"] = "0" * 64
    (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Manifest eval hash"):
        load_data(data)


def test_repeated_prompt_across_splits_is_rejected(tmp_path):
    data = _prepared(tmp_path)
    row = _row("two", "b", "Question one?")
    path = data / "eval.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    manifest["splits"]["eval"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Identical initial prompts"):
        load_data(data)


class FakeTokenizer:
    eos_token_id = 999

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, enable_thinking):
        assert tokenize and add_generation_prompt and not enable_thinking
        return [1, 2, 3, 4]

    def __call__(self, text, *, add_special_tokens):
        return {"input_ids": [ord(character) for character in text]}


def test_completion_only_masking_and_no_truncation():
    tokenizer = FakeTokenizer()
    messages = _row("one", "a", "Question one?")["messages"]
    # A Qwen-like generation prompt can insert a think marker that is absent
    # from a completed assistant render. Only the generation prompt is used.
    class ThinkingPrefixTokenizer(FakeTokenizer):
        def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, enable_thinking):
            if add_generation_prompt:
                return [1, 2, 777, 778]  # synthetic <think></think> prefix
            return [1, 2, 3, 4]  # different full-assistant rendering
    encoded = tokenize_example(ThinkingPrefixTokenizer(), messages, 1024)
    assert encoded["input_ids"][:4] == [1, 2, 777, 778]
    assert encoded["labels"][:4] == [-100] * 4
    assert encoded["input_ids"][-1] == encoded["labels"][-1] == 999
    with pytest.raises(ValueError, match="no truncation"):
        tokenize_example(ThinkingPrefixTokenizer(), messages, 16)
    assert tokenize_example(tokenizer, messages, 1024)["labels"][:4] == [-100] * 4


def test_protocol_scores_distinguish_tool_args_and_answer_coverage():
    target = '{"tool":"battles","arguments":{"fresh_minutes":5}}'
    exact = score_prediction(target, '{"arguments":{"fresh_minutes":5},"tool":"battles"}')
    assert exact["tool_exact"] and exact["action_exact"]
    wrong = score_prediction(target, '{"tool":"battles","arguments":{"fresh_minutes":10}}')
    assert not wrong["tool_exact"]
    answer = score_prediction('{"answer":"Observed in the local data."}', '{"answer":"Different claim."}')
    assert answer["answer_present"] and not answer["answer_exact"]
    assert not score_prediction(target, "not JSON")["valid"]
    assert summarize([{"target_kind": "tool", "scores": exact}])["tool_and_args_exact_rate"] == 1


def test_output_overlap_and_export_preflight(tmp_path):
    data = _prepared(tmp_path)
    with pytest.raises(ValueError, match="overlaps"):
        check_output(data / "adapter", data)
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    with pytest.raises(ValueError, match="requires run_metadata"):
        export_main(["--adapter", str(adapter), "--output", str(tmp_path / "merged")])

import hashlib
import json
from pathlib import Path

import pytest

from llm_training.data import expand_examples, prepare_dataset, read_records, split_records, validate_record
from llm_training.protocol import ProtocolError, parse_action


EXAMPLES = Path(__file__).resolve().parents[1] / "llm_training" / "examples" / "starter.jsonl"


class FakeTokenizer:
    chat_template = "fake-chat-template"
    init_kwargs = {"_commit_hash": "test-revision"}
    eos_token_id = 99

    def apply_chat_template(self, messages, **kwargs):
        assert kwargs == {"tokenize": True, "add_generation_prompt": True, "enable_thinking": False}
        return list(range(sum(len(message["content"].split()) + 2 for message in messages)))

    def __call__(self, content, **kwargs):
        assert kwargs == {"add_special_tokens": False}
        return {"input_ids": list(range(len(content.split())))}


def test_starter_examples_prepare_with_real_template_api(tmp_path):
    records, source_hash = read_records(EXAMPLES)
    assert len(records) == 11
    assert len({record["group"] for record in records}) == 11
    manifest = prepare_dataset(EXAMPLES, tmp_path / "prepared", FakeTokenizer())
    output = tmp_path / "prepared"
    assert manifest["input_sha256"] == source_hash
    assert manifest["tokenizer_revision"] == "test-revision"
    assert manifest["chat_template_sha256"] == hashlib.sha256(b"fake-chat-template").hexdigest()
    assert manifest["chat_template"] == {"enable_thinking": False, "add_generation_prompt": True,
                                         "sequence_format": "generation_prompt + encoded final assistant content + eos"}
    assert manifest["source_records"] == 11
    assert manifest["expanded_records"] == 20
    assert sum(manifest["splits"][split]["records"] for split in ("train", "eval")) == 20
    prepared = []
    for split in ("train", "eval"):
        raw = (output / f"{split}.jsonl").read_bytes()
        assert manifest["splits"][split]["sha256"] == hashlib.sha256(raw).hexdigest()
        rows = [json.loads(line) for line in raw.splitlines()]
        prepared.extend((split, row) for row in rows)
        assert all(row["messages"][0]["role"] == "system" for row in rows)
        assert all(row["messages"][-1]["role"] == "assistant" for row in rows)
        assert sum(manifest["record_tokens"][row["id"]] for row in rows) == manifest["splits"][split]["tokens"]
    assert set(manifest["splits"]["train"]["groups"]).isdisjoint(manifest["splits"]["eval"]["groups"])
    tool_targets = [row for _, row in prepared if "tool" in parse_action(row["messages"][-1]["content"])]
    assert len(tool_targets) == 9
    assert {row["group"] for row in tool_targets} <= {row["group"] for _, row in prepared}
    assert all(len({split for split, row in prepared if row["group"] == group}) == 1
               for group in {row["group"] for _, row in prepared})


def test_starter_tool_observations_match_runtime_envelope():
    records, _ = read_records(EXAMPLES)
    for record in records:
        messages = record["messages"]
        if len(messages) == 3:
            continue
        action = parse_action(messages[2]["content"])
        observation = json.loads(messages[3]["content"])["tool_result"]
        assert observation["tool"] == action["tool"]
        assert isinstance(observation["summary"], str)
        assert isinstance(observation["data"], dict)
        assert observation["data"]["data_mode"] == "demo"
    grounded = next(row for row in records if row["id"] == "activity_observation_timing")
    data = json.loads(grounded["messages"][3]["content"])["tool_result"]["data"]
    assert data["last_event_at"] < data["last_observed_at"]


def test_overlength_rejects_without_publishing(tmp_path):
    output = tmp_path / "prepared"
    with pytest.raises(ValueError, match="no truncation"):
        prepare_dataset(EXAMPLES, output, FakeTokenizer(), max_length=2)
    assert not output.exists()


def test_input_bounds_and_existing_output(tmp_path):
    large = tmp_path / "large.jsonl"
    large.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="16 MiB"):
        read_records(large)
    output = tmp_path / "existing"
    output.mkdir()
    (output / "notes.txt").write_text("keep")
    with pytest.raises(ValueError, match="absent or empty"):
        prepare_dataset(EXAMPLES, output, FakeTokenizer())
    assert (output / "notes.txt").read_text() == "keep"


def test_existing_empty_output_is_published(tmp_path):
    output = tmp_path / "prepared"
    output.mkdir()
    prepare_dataset(EXAMPLES, output, FakeTokenizer())
    assert (output / "manifest.json").is_file()


def test_group_holdout_rejects_duplicate_prompt_across_groups():
    records, _ = read_records(EXAMPLES)
    copy = dict(records[0], id="copy", group="another-group")
    with pytest.raises(ValueError, match="identical initial prompts"):
        split_records(records + [copy])


def test_prefix_expansion_keeps_original_id_and_tool_target():
    records, _ = read_records(EXAMPLES)
    expanded = expand_examples(records)
    original = next(row for row in records if row["id"] == "activity_observation_timing")
    group_rows = [row for row in expanded if row["group"] == original["group"]]
    assert len(group_rows) == 2
    assert any(row["id"] == original["id"] for row in group_rows)
    tool_row = next(row for row in group_rows if row["id"] != original["id"])
    assert tool_row["messages"] == original["messages"][:3]
    assert parse_action(tool_row["messages"][-1]["content"])["tool"] == "activity"


@pytest.mark.parametrize("bad", [
    '{"tool":"forecast","arguments":{"extra":1}}',
    '{"tool":"activity","arguments":{"question":"x","hours":true}}',
    '{"tool":"activity","arguments":{"question":"x","location":{"kind":[],"id":1}}}',
    '{"tool":"battles","arguments":{"space":[]}}',
    '{"answer":"x","answer":"y"}',
    '{"tool":"filesystem","arguments":{}}',
    '```json\n{"answer":"x"}\n```',
    '{"answer":',
])
def test_malformed_action_is_protocol_error(bad):
    with pytest.raises(ProtocolError):
        parse_action(bad)


def test_invalid_role_or_assistant_action_rejected():
    records, _ = read_records(EXAMPLES)
    bad = json.loads(json.dumps(records[0]))
    bad["messages"][1]["role"] = "tool"
    with pytest.raises(ValueError, match="invalid role"):
        validate_record(bad)
    bad = json.loads(json.dumps(records[0]))
    bad["messages"][-1]["content"] = "plain answer"
    with pytest.raises(ValueError, match="invalid assistant action"):
        validate_record(bad)

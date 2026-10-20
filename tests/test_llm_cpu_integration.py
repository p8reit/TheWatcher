"""Optional ML-stack check: tiny random weights, no model download or CUDA."""

import json
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("peft")


def test_tiny_qwen_adapter_can_merge_and_reload(tmp_path):
    from peft import LoraConfig, get_peft_model
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast, Qwen3Config, Qwen3ForCausalLM
    from llm_training.data import prepare_dataset
    from llm_training.export import main as export
    from llm_training.train import main as train, tokenize_example

    vocab = {"[UNK]": 0, "[PAD]": 1, "[EOS]": 2, "system": 3, "user": 4, "assistant": 5}
    backend = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]", eos_token="[EOS]")
    # Exercise the pipeline with an actual tokenizer, without downloading Qwen's tokenizer.
    tokenizer.chat_template = (
        "{% for message in messages %}{{ message['role'] + '\\n' + message['content'] + '[EOS]\\n' }}"
        "{% endfor %}{% if add_generation_prompt %}{{ 'assistant\\n' }}{% endif %}")
    base_path = tmp_path / "base"
    base = Qwen3ForCausalLM(Qwen3Config(
        vocab_size=len(vocab), hidden_size=32, intermediate_size=64, num_hidden_layers=1,
        num_attention_heads=4, num_key_value_heads=2, head_dim=8,
        max_position_embeddings=2048, use_sliding_window=False))
    base.save_pretrained(base_path)
    tokenizer.save_pretrained(base_path)
    prepared = tmp_path / "prepared"
    examples = Path(__file__).parents[1] / "llm_training/examples/starter.jsonl"
    prepare_dataset(examples, prepared, tokenizer, tokenizer_name=str(base_path), max_length=2048)
    record = json.loads((prepared / "train.jsonl").read_text().splitlines()[0])
    encoded = tokenize_example(tokenizer, record["messages"], 2048)
    output = base(**{key: torch.tensor([value]) for key, value in encoded.items()})
    assert torch.isfinite(output.loss)
    output.loss.backward()

    adapter = get_peft_model(
        Qwen3ForCausalLM.from_pretrained(base_path),
        LoraConfig(r=2, lora_alpha=4, target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"))
    adapter_path = tmp_path / "adapter"
    adapter.save_pretrained(adapter_path)
    tokenizer.save_pretrained(adapter_path)
    config = json.loads((adapter_path / "adapter_config.json").read_text())
    (adapter_path / "run_metadata.json").write_text(json.dumps({
        "base_model": config["base_model_name_or_path"], "base_revision": None}))
    merged_path = tmp_path / "merged"
    assert export(["--adapter", str(adapter_path), "--output", str(merged_path),
                   "--local-files-only"]) == 0
    merged = Qwen3ForCausalLM.from_pretrained(merged_path, local_files_only=True)
    assert merged.config.model_type == "qwen3"
    # LoRA starts at zero; merging must preserve the base projection.
    assert torch.allclose(merged.model.layers[0].self_attn.q_proj.weight.float(),
                          base.model.layers[0].self_attn.q_proj.weight.float(),
                          atol=1e-4)
    assert train(["--data", str(prepared), "--output", str(tmp_path / "dry-run"),
                  "--model", str(base_path), "--max-length", "2048", "--dry-run"]) == 0

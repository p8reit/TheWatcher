# Local LLM training and evaluation

The Watcher can use a downloaded Ollama model for local analyst replies. The optional agent calls a small, fixed set of read-only application tools and is enabled only when both `LLM_ENABLED=true` and `LLM_AGENT_ENABLED=true`. Training is a separate, manual CUDA workflow. Opening the website never downloads a model or starts fine-tuning. The **Train & evaluate** controls for activity and NPC forecasts train statistical models; they use 0 LLM tokens and do not affect the Ollama model.

This recipe targets Windows 11 with WSL2, Docker Desktop/Engine with Compose v2 and NVIDIA GPU support, an RTX 3060 with 12 GB VRAM, and 32 GB host RAM. It uses `Qwen/Qwen3-4B`, 4-bit NF4 QLoRA, LoRA rank 8, a 1,024-token ceiling, batch size 1, and gradient accumulation 16. The trainer uses Hugging Face Transformers, PEFT and PyTorch. The training image is pinned to PyTorch 2.6.0 with CUDA 12.4; `requirements-training.txt` pins the other training packages. It does not compile CUDA extensions during build. These are starting settings, not a performance or quality claim.

## Prepare the host

1. Install a current Windows NVIDIA driver with WSL CUDA support, enable WSL2, and install Docker Desktop with WSL integration (or configure Docker Engine and the NVIDIA Container Toolkit inside WSL). Check `nvidia-smi` from WSL. The Windows driver supplies the driver interface; do not install a Linux display driver inside WSL.
2. Check GPU access in a container before building the trainer:

   ```sh
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```

   Resolve driver, WSL integration or Docker GPU errors before training. A container that cannot see the GPU cannot run this QLoRA recipe.
3. Keep the repository in the WSL ext4 filesystem, such as `~/TheWatcher`, rather than `/mnt/c`, for faster model loading and checkpoint writes. Allow at least 40 GB free for the base weights, cache, adapter, merged model and GGUF conversion files. Allow WSL roughly 20 GB RAM if other Windows applications leave room; 24 GB may help with a CPU merge, but keep enough for Windows. Restart WSL after changing `.wslconfig`. Close other GPU applications, including Ollama, during training. A 12 GB card has little headroom when another model is resident.

All commands below run from the repository root in WSL. `compose.training.yaml` is its own Compose project and mounts only `./data/llm` and a named Hugging Face cache. It neither mounts nor connects to the web database. The trainer runs as UID 1000; make `data/llm` writable by that user if your WSL account uses another UID. Model weights, cache and outputs are ignored by Git and the Docker build context.

## Download, prepare, train

First create the local workspace and build the image. The build installs Python packages; it does not fetch Qwen weights.

```sh
mkdir -p data/llm
docker compose -f compose.training.yaml build trainer
docker compose -f compose.training.yaml run --rm trainer python -c 'from huggingface_hub import snapshot_download; snapshot_download("Qwen/Qwen3-4B")'
```

The last command downloads public Qwen weights and tokenizer files into the named cache. Review the model's license and current upstream revision before using it. If you need reproducible training, record and use a specific Hub revision in the download and `train --revision` commands. Subsequent data preparation, training, evaluation and export should use the already downloaded files.

For a pipeline exercise before curating data, prepare the fictional starter examples and run only two optimizer steps into a disposable directory:

```sh
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.data \
  --input /workspace/llm_training/examples/starter.jsonl \
  --output /workspace/data/llm/smoke-prepared --local-files-only
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.train \
  --data /workspace/data/llm/smoke-prepared \
  --output /workspace/data/llm/smoke-adapter \
  --model Qwen/Qwen3-4B --max-steps 2 --local-files-only
```

This uses GPU time and produces a test artifact, not an analyst model to deploy.

The checked-in `llm_training/examples/starter.jsonl` contains fictional examples to show the JSONL format and exercise the pipeline. It is far too small and synthetic to produce a useful deployed model. Make a manually reviewed `data/llm/curated.jsonl` with distinct scenario groups covering EVE terminology, requested tool selection, tool-result interpretation, uncertainty, refusals to infer login or current location, and answers that cite only observed facts. Each row needs a unique `id`, a `group` shared by related scenarios, and `messages` ending in the target assistant action. Keep real player identities and raw production killmail collections out of training examples. The preparation command holds out whole groups, rejects duplicate first prompts across groups, and counts actual Qwen chat-template tokens; it rejects examples exceeding 1,024 tokens instead of silently truncating them.

```sh
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.data \
  --input /workspace/data/llm/curated.jsonl \
  --output /workspace/data/llm/prepared \
  --tokenizer Qwen/Qwen3-4B --max-length 1024 --local-files-only
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.train \
  --data /workspace/data/llm/prepared \
  --output /workspace/data/llm/adapter \
  --model Qwen/Qwen3-4B --max-length 1024 --local-files-only --dry-run
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.train \
  --data /workspace/data/llm/prepared \
  --output /workspace/data/llm/adapter \
  --model Qwen/Qwen3-4B --max-length 1024 --local-files-only
```

Inspect `data/llm/prepared/manifest.json` before training. Preparation creates one supervised example per assistant turn, so both tool requests and final answers receive training. Related turns stay in the same scenario group and split. The manifest records source and expanded counts, scenario groups, token totals, file hashes and per-example token counts. Tokens include the non-thinking generation prompt, the final assistant content and its end-of-turn token; only the final assistant completion contributes to training loss. The dry run checks prepared files and paths without loading CUDA. `--max-steps N` is available for an intentionally short training smoke run; use a fresh output directory for each run. If VRAM runs out, free the GPU and retry; do not infer that a short or successful run improves analyst quality.

## Evaluate before switching inference

Evaluate the adapter against the held-out groups and compare it with the unmodified base model on the same examples. Review both machine-readable results and a manual set of realistic questions, especially tool choice, unsupported claims, stale or incomplete evidence, and prompt injection in tool results. A lower loss or valid JSON alone does not establish usefulness. Do not promote a model that regresses on these checks. Keep the original Ollama tag available for a quick rollback.

```sh
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.evaluate \
  --data /workspace/data/llm/prepared \
  --adapter /workspace/data/llm/adapter \
  --output /workspace/data/llm/evaluation.json \
  --base-model Qwen/Qwen3-4B --baseline --local-files-only
docker compose -f compose.training.yaml run --rm trainer python -m llm_training.export \
  --adapter /workspace/data/llm/adapter \
  --output /workspace/data/llm/merged --local-files-only
```

The evaluation JSON reports base and adapter results on the same held-out examples. Inspect the results before export. Export merges the adapter into a full Hugging Face model and writes a GGUF Modelfile template. The CPU merge path has been smoke-tested on a tiny Qwen3 model; full 4B CUDA training, GGUF conversion and Ollama import have not been validated here. A full merge can need much more system RAM and temporary disk than adapter training; stop Ollama and free space first.

Tool accuracy requires the exact expected tool and arguments. Answer presence means a nonempty answer field; exact answer agreement compares text. Neither measures factual correctness.

For an Ollama GGUF model, convert the merged output with llama.cpp. The pinned `b5920` source tag includes a `Qwen3ForCausalLM` converter. Install `git`, `cmake` and a C++ compiler in WSL. The Python conversion runs in the trainer image on CPU; CMake builds only the host's CPU quantizer. The full precision GGUF is a temporary intermediate and may need another 8 GB or more of disk space.

```sh
git clone --branch b5920 --depth 1 https://github.com/ggml-org/llama.cpp.git data/llm/llama.cpp
cmake -S data/llm/llama.cpp -B data/llm/llama.cpp/build -DGGML_CUDA=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build data/llm/llama.cpp/build --target llama-quantize -j 4
docker compose -f compose.training.yaml run --rm trainer python \
  /workspace/data/llm/llama.cpp/convert_hf_to_gguf.py /workspace/data/llm/merged \
  --outfile /workspace/data/llm/watcher-eve-f16.gguf --outtype f16
data/llm/llama.cpp/build/bin/llama-quantize data/llm/watcher-eve-f16.gguf \
  data/llm/merged/watcher-eve-Q4_K_M.gguf Q4_K_M
ollama create watcher-eve -f data/llm/merged/Modelfile.gguf.template
ollama run watcher-eve
```

Repeat the manual analyst checks against the actual Ollama model before changing the app. The GGUF quantization and Ollama runtime can change outputs from the in-process adapter evaluation.

To opt into local agent inference, set these values in the app's `.env`, using the exact installed Ollama tag:

```dotenv
LLM_ENABLED=true
LLM_AGENT_ENABLED=true
OLLAMA_MODEL=watcher-eve:latest
```

Check `ollama list` for the exact installed tag, then recreate the web container with the Compose project you already use, for example `docker compose -p new-eden-live up -d --build --force-recreate web`. The web container reaches an existing host Ollama service at `OLLAMA_URL`; it does not run Ollama or mount training output. To roll back, restore the previous `OLLAMA_MODEL` tag and recreate the web container. Keep Ollama on a private interface. The local inference budget in `.env` is independent of model training and of any ChatGPT subscription.

## Implementation checks

The cloud checks passed 210 application and integration tests, JavaScript syntax validation, training Compose configuration, and a browser smoke test using a simulated agent reply. An additional CPU environment with the pinned Transformers, PEFT and PyTorch versions passed the optional tiny Qwen3 test: supervised forward/backward, adapter save, FP16 merge and checkpoint reload. No pretrained weights were needed for that test.

The cloud environment has no NVIDIA GPU, and its network policy blocked the Qwen tokenizer download. It has not run the full training image, actual Qwen3-4B training or generation, GGUF conversion, or Ollama deployment. The target-host smoke procedure above is the next validation step. The starter examples establish workflow behavior, not EVE expertise or model quality.

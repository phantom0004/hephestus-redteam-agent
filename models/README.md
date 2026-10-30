# Downloading and loading local models

No model weights are included in this repository. Hephestus uses llama-cpp-python
to load a compatible **single-file text-generation GGUF** directly. The browser
model library scans the application's `models/` folder.

## Recommended first download

From the project root:

```sh
python setup.py --model-only --download-model
```

This downloads `qwen2.5-coder-1.5b-instruct-q4_k_m.gguf` (approximately 1.12 GB)
into this folder. Setup checks the pinned revision's size, GGUF header and SHA-256;
the values are defined in `../setup.py`. For dependency installation too, use
`python setup.py --download-model`.

The model is an official Qwen coding-focused Instruct model under Apache-2.0.
It is a small starting point for learning the workflow, not a guarantee of correct
commands. Read the [model card and license](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF).

The master's research beta used a different model/setup: Qwen2.5-Coder-14B-Instruct
Q5_K_M served on a hosted GPU. Neither this 1.5B download nor V1/V2 was evaluated
by that study. See [research background](../docs/RESEARCH-BACKGROUND.md).

## Choosing another model

Prefer a **Coder / Code + Instruct / Chat + GGUF** model for an initial command
writing comparison. General-purpose Instruct models can work too. Avoid Base-only
models as a beginner. `Q4_K_M` is a practical starting quantization; `Q5_K_M` or
`Q8_0` require more memory. Larger parameter counts such as 7B or 14B also demand
more resources. Memory use exceeds file size and grows with context length.

Find models on [Hugging Face](https://huggingface.co/models). Open the model's
**Files and versions**, choose one complete `.gguf` quantization and download the
actual file with its download control. For the first model, use the
[official file listing](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/tree/main)
and select exactly `qwen2.5-coder-1.5b-instruct-q4_k_m.gguf`.

Choose a supported architecture and instruction/chat template. Prefer a trusted
publisher and review the model's license and resource requirements. A `.gguf`
extension alone does not guarantee support by the pinned runtime. For this simple
workflow, avoid split shards (`-00001-of-...`), LoRA adapters, embedding/reranker
models and `mmproj` vision files. `.safetensors`, `.bin`, GPTQ, AWQ and EXL2 weights
cannot be loaded directly here. Renaming them is not conversion. You normally need
only the self-contained GGUF, not separate tokenizer files, the full repository,
Ollama or a cloud account. Verify publisher checksums when available; manual
files do not automatically receive setup's pinned verification.

## Load it in Hephestus

1. Finish the download and put the complete `.gguf` in this folder. Extract an
   archive first if necessary; do not leave a `.part`, ZIP or saved HTML page.
2. Start Hephestus, open **Models**, click **Rescan** and use **Load**.
3. Wait for **Loaded**, then try “Print a greeting” in Local mode. Review the
   proposed command before approving it. To switch models, stop the task and
   eject the current model first.
4. If loading fails, check runtime/template support. If memory runs out, lower
   context/batch size, reduce GPU layers or use a smaller model.

These paths describe a source checkout. Installed packages use a per-user data
folder, configurable with `HEPHESTUS_DATA_DIR`; see the
[main model guide](../README.md#models-and-hardware) and
[configuration guide](../README.md#configuration-and-startup).
No model loads automatically, and the Models screen does not download from the Hub.

Keep weights out of Git and release archives. This project's MIT license does not
relicense external models.

# Models and hardware

Weights are external dependencies and are absent from this release. Local inference requires a downloaded or separately provisioned model.

## Optional quick-start model

Setup offers the official [Qwen2.5-Coder-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF) `Q4_K_M` file. This is a setup convenience, not a validated replacement for models used in the original experiments.

| Property | Pinned value |
| --- | --- |
| Filename | `qwen2.5-coder-1.5b-instruct-q4_k_m.gguf` |
| Revision | `f86cb2c1fa58255f8052cc32aeede1b7482d4361` |
| Size | `1,117,320,768` bytes, approximately `1.12 GB` |
| SHA-256 | `cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046` |
| License | Apache-2.0; review the upstream license and model card. |
| Destination | Project `models/`. |

The downloader streams to a temporary file, validates size/hash/GGUF header, then renames it into place. Incomplete files are removed on failure. A verified existing quick-start file is reused; an existing file that fails verification is not overwritten. Setup requires at least its disk allowance of about 1.25 GB. Inference memory exceeds file size and depends on context and runtime settings.

```sh
python setup.py --model-only --download-model
```

The hash checks integrity against the expected upstream artifact; it does not guarantee safe proposals.

## Supply your own model

Use one instruct/chat GGUF with an architecture and template supported by the installed llama.cpp runtime. A `.gguf` extension alone is insufficient. Split shards and non-GGUF formats are not supported by this setup flow.

```sh
hephestus "List the current directory" --dry-run --model "/path/to/model.gguf"
```

Alternatively set `LLM_MODEL` or YAML `llm.model`. YAML paths resolve beside the configuration file. Set `llm.chat_format` only if metadata is insufficient and the runtime supports the format. Review source, license, checksum, memory requirements, and architecture before loading files into a native runtime.

The original project used Qwen2.5-Coder-14B-Instruct in Q5_K_M format, with a model file around 10.5 GB. You can supply that file with the same model setting if your hardware supports it. It requires substantially more memory than the starter model. The starter is intended to verify setup; expect weaker planning and syntax reliability from a smaller model.

## CPU first, optional acceleration

The default wheel backend is `cpu`, with `llm.gpu_layers: 0`. Establish a working baseline first.

| Setup choice | Intended runtime |
| --- | --- |
| `cpu` | CPU. |
| `cu130`, `cu132` | Compatible NVIDIA CUDA driver/runtime. |
| `metal` | Supported Apple Metal platform. |
| `vulkan` | Supported Vulkan platform and driver. |
| `rocm72` | Compatible AMD ROCm platform. |

Availability depends on the pinned runtime version, OS, architecture, Python version, and upstream wheels. Selecting an index does not install drivers or promise wheel availability. Setup requests an inference binary and fails instead of silently compiling it. Consult [upstream installation documentation](https://github.com/abetlen/llama-cpp-python#installation) for supported combinations and deliberate manual builds.

With a compatible GPU build, set `llm.gpu_layers` to a positive count or `-1` for requested full offload. Tune context, batch size, threads, generation tokens, and history budget within RAM/VRAM. Reduce context, batch, offload, or model size after memory failures. See [configuration](configuration.md).

On Windows, `CUDA_PATH` can provide native DLL directories. CPU operation does not need CUDA. Do not add untrusted native-library search paths.

## Manual installation

Create and activate a Python 3.10+ virtual environment first. CPU dependency installation is equivalent to:

```sh
python -m pip install --upgrade pip
python -m pip install --only-binary=llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu -r requirements.txt
python -m pip install --no-deps -e .
python -m pip check
```

Choose another compatible index deliberately. Direct dependencies are pinned; transitive dependencies and platform wheels can still differ between installations.

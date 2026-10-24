# External model files

Model weights are not shipped in this release. This directory is the default local destination for the optional verified quick-start GGUF:

```sh
python setup.py --model-only --download-model
```

Setup offers the official Qwen2.5-Coder-1.5B-Instruct Q4_K_M model, approximately 1.12 GB, under its own Apache-2.0 license. Its pinned revision, size, and SHA-256 are documented in [model setup](../docs/models.md).

You may supply a single instruct GGUF supported by the installed llama.cpp runtime using `--model`, `LLM_MODEL`, or `llm.model` in YAML. Review the external model's license, provenance, architecture, template, and resource requirements.

Keep downloaded weights out of Git and release archives. The code's MIT license does not relicense external models.

"""Lazy local GGUF inference with explicit hardware and prompt budgets."""

import os
import threading
from pathlib import Path

from hephestus import config

_dll_directory_handles = []
_llama_class = None
_model = None
_model_settings = None
_model_lock = threading.RLock()


def _load_llama_class():
    global _llama_class
    if _llama_class is not None:
        return _llama_class
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        cuda = os.getenv("CUDA_PATH")
        if cuda:
            for directory in (Path(cuda) / "bin", Path(cuda) / "bin" / "x64"):
                if directory.is_dir():
                    _dll_directory_handles.append(os.add_dll_directory(str(directory)))
    try:
        from llama_cpp import Llama
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Cannot load llama-cpp-python. Run python setup.py; see README for CPU/GPU installation."
        ) from exc
    _llama_class = Llama
    return Llama


def validate_runtime(model_path=None):
    path = Path(model_path or config.LLM_MODEL).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"GGUF model not found: {path}. Run python setup.py or set LLM_MODEL to your own GGUF."
        )
    with path.open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError(
                "Model file is not GGUF (check for an incomplete download or Git LFS pointer)."
            )
    _load_llama_class()


def model_is_available(model_path=None):
    try:
        validate_runtime(model_path)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def _get_model():
    global _model, _model_settings
    settings = (
        config.LLM_MODEL,
        config.LLM_CONTEXT_SIZE,
        config.LLM_GPU_LAYERS,
        config.LLM_THREADS,
        config.LLM_BATCH_SIZE,
        config.LLM_CHAT_FORMAT,
    )
    with _model_lock:
        if _model is not None and settings == _model_settings:
            return _model
        validate_runtime()
        if _model is not None:
            _model.close()
            _model = None
        options = dict(
            model_path=config.LLM_MODEL,
            n_ctx=config.LLM_CONTEXT_SIZE,
            n_gpu_layers=config.LLM_GPU_LAYERS,
            n_threads=config.LLM_THREADS,
            n_threads_batch=config.LLM_THREADS,
            n_batch=config.LLM_BATCH_SIZE,
            use_mmap=True,
            verbose=False,
        )
        if config.LLM_CHAT_FORMAT:
            options["chat_format"] = config.LLM_CHAT_FORMAT
        _model = _load_llama_class()(**options)
        # DiskCache's pickle-backed persistence is deliberately disabled.
        _model.set_cache(None)
        _model_settings = settings
        return _model


def create_chat_completion(messages, max_tokens, temperature, top_p):
    # llama.cpp state is shared; tokenization and generation must be serialized.
    with _model_lock:
        model = _get_model()
        prompt = "\n".join(message["content"] for message in messages)
        tokens = model.tokenize(prompt.encode("utf-8"))
        if len(tokens) + max_tokens + 128 >= config.LLM_CONTEXT_SIZE:
            raise ValueError(
                "Prompt exceeds the configured context window. Reduce goal/context/history or increase llm.context_size."
            )
        return model.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )

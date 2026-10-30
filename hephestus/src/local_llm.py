"""Lazy local GGUF inference with explicit hardware and prompt budgets."""

import os
import atexit
import struct
import threading
from pathlib import Path

from hephestus import config
from hephestus.src.inference_process import InferenceProcess

_dll_directory_handles = []
_llama_class = None
_model = None
_model_settings = None
_effective_gpu_layers = None
_model_lock = threading.RLock()

# GGUF scalar value types -> struct format, used to read model metadata without
# loading the weights. See the GGUF specification for the type enumeration.
_GGUF_SCALARS = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
    4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
    10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}
_GGUF_STRING = 8
_GGUF_ARRAY = 9


def _cuda_library_directories() -> tuple[Path, ...]:
    """Find CUDA DLL folders even when CUDA_PATH was not exported by the shell."""
    if os.name != "nt":
        return ()
    roots = []
    configured = os.getenv("CUDA_PATH")
    if configured:
        roots.append(Path(configured))
    install_root = Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / "NVIDIA GPU Computing Toolkit" / "CUDA"
    roots.extend(sorted(install_root.glob("v*"), reverse=True))
    directories = []
    for root in roots:
        for directory in (root / "bin", root / "bin" / "x64"):
            if directory.is_dir() and directory not in directories:
                directories.append(directory)
    return tuple(directories)


def _load_llama_class():
    global _llama_class
    if _llama_class is not None:
        return _llama_class
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for directory in _cuda_library_directories():
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
    return runtime_status(model_path)[0]


def runtime_status(model_path=None):
    """Return a safe, operator-actionable runtime result without loading the model."""
    try:
        validate_runtime(model_path)
        return True, ""
    except (OSError, ValueError, RuntimeError) as exc:
        return False, str(exc)


def read_model_metadata(model_path=None):
    """Read key GGUF metadata (layer count, dims, size) without loading weights."""
    path = Path(model_path or config.LLM_MODEL).expanduser()
    try:
        size_bytes = path.stat().st_size
        with path.open("rb") as handle:
            if handle.read(4) != b"GGUF":
                return {}
            version = struct.unpack("<I", handle.read(4))[0]
            if version < 2:
                return {"size_bytes": size_bytes}
            struct.unpack("<Q", handle.read(8))  # tensor_count (unused)
            kv_count = struct.unpack("<Q", handle.read(8))[0]

            def read_string():
                length = struct.unpack("<Q", handle.read(8))[0]
                return handle.read(length).decode("utf-8", "replace")

            def read_scalar(value_type):
                fmt, size = _GGUF_SCALARS[value_type]
                return struct.unpack(fmt, handle.read(size))[0]

            def skip_value(value_type):
                if value_type == _GGUF_STRING:
                    handle.seek(struct.unpack("<Q", handle.read(8))[0], os.SEEK_CUR)
                elif value_type == _GGUF_ARRAY:
                    element_type = struct.unpack("<I", handle.read(4))[0]
                    count = struct.unpack("<Q", handle.read(8))[0]
                    for _ in range(count):
                        if element_type in (_GGUF_STRING, _GGUF_ARRAY):
                            skip_value(element_type)
                        else:
                            handle.seek(_GGUF_SCALARS[element_type][1], os.SEEK_CUR)
                else:
                    handle.seek(_GGUF_SCALARS[value_type][1], os.SEEK_CUR)

            meta = {}
            for _ in range(kv_count):
                key = read_string()
                # Tokenizer vocabularies follow the model keys and can hold
                # hundreds of thousands of strings; nothing after them is needed.
                if key.startswith("tokenizer.") and "general.architecture" in meta:
                    break
                value_type = struct.unpack("<I", handle.read(4))[0]
                if value_type == _GGUF_STRING:
                    meta[key] = read_string()
                elif value_type in _GGUF_SCALARS:
                    meta[key] = read_scalar(value_type)
                else:
                    skip_value(value_type)
    except (OSError, struct.error, KeyError, ValueError, IndexError):
        return {}

    arch = meta.get("general.architecture", "")

    def as_int(key):
        value = meta.get(f"{arch}.{key}")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    file_type = meta.get("general.file_type")
    return {
        "arch": arch,
        "size_bytes": size_bytes,
        "n_layers": as_int("block_count"),
        "n_embd": as_int("embedding_length"),
        "n_head": as_int("attention.head_count"),
        "n_head_kv": as_int("attention.head_count_kv"),
        "context_length": as_int("context_length"),
        "name": str(meta.get("general.name") or "")[:120],
        "size_label": str(meta.get("general.size_label") or "")[:24],
        "quantization": _GGUF_FILE_TYPES.get(file_type, "") if isinstance(file_type, int) else "",
    }


# general.file_type -> conventional quantization label (llama.cpp enum).
_GGUF_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
    16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S",
    22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
}


def suggest_gpu_layers(context_size=None, model_path=None):
    """Conservatively size GPU offload from free VRAM and the model's shape.

    Returns ``(layers, info)``. ``layers`` is -1 (offload all), 0 (CPU only) or a
    positive partial count. The estimate is intentionally biased to leave VRAM
    headroom; the loader's OOM fallback steps it down further if even this is
    too high, so automatic sizing can never leave the operator unable to load.
    """
    from hephestus.src import telemetry

    snapshot = telemetry.gpu_snapshot()
    if not snapshot.get("available") or not snapshot.get("devices"):
        return 0, {"available": False, "reason": "No NVIDIA GPU detected; using CPU"}
    device = snapshot["devices"][0]
    total = int(device.get("memory_total") or 0)
    free = max(0, total - int(device.get("memory_used") or 0))
    meta = read_model_metadata(model_path)
    size_mib = (meta.get("size_bytes") or 0) / 1048576
    n_layers = meta.get("n_layers") or 0
    context = int(context_size or config.LLM_CONTEXT_SIZE)
    info = {
        "available": True,
        "free_mib": free,
        "total_mib": total,
        "model_mib": round(size_mib),
        "model_layers": n_layers,
    }
    if free <= 0 or size_mib <= 0:
        return 0, {**info, "layers": 0, "reason": "VRAM or model size unavailable; using CPU"}
    if not n_layers:
        if size_mib < free * 0.55:
            return -1, {**info, "layers": -1, "reason": "Model fits comfortably; offloading all layers"}
        return 0, {**info, "layers": 0, "reason": "Layer count unknown; using CPU to avoid OOM"}

    per_layer_weight = size_mib / n_layers
    n_embd = meta.get("n_embd") or 0
    n_head = meta.get("n_head") or 0
    n_head_kv = meta.get("n_head_kv") or n_head
    if n_embd and n_head and n_head_kv:
        kv_dim = (n_embd / n_head) * n_head_kv
    else:
        kv_dim = n_embd or 4096
    kv_per_layer = (2 * context * kv_dim * 2) / 1048576  # fp16 K and V per layer
    per_layer = per_layer_weight + kv_per_layer
    # Reserve for non-offloaded tensors (embeddings/output) and CUDA compute
    # buffers, then spend a conservative share of the remaining free VRAM. The
    # loader's OOM fallback steps down further if this is still too optimistic.
    reserve = max(1024, size_mib * 0.08) + 768
    budget = free * 0.80 - reserve
    if per_layer <= 0 or budget <= 0:
        return 0, {**info, "layers": 0, "reason": "Not enough free VRAM; using CPU"}
    layers = max(0, min(int(budget // per_layer), n_layers))
    full_need = size_mib + kv_per_layer * n_layers + reserve
    if layers >= n_layers and full_need < free * 0.92:
        return -1, {**info, "layers": -1, "reason": "Full model fits; offloading all layers"}
    return layers, {
        **info,
        "layers": layers,
        "per_layer_mib": round(per_layer),
        "reason": f"Partial offload sized to {free} MiB free VRAM",
    }


def _gpu_fallback_sequence(requested):
    """Decreasing offload counts to retry if a chosen count fails to allocate."""
    if requested == 0:
        return [0]
    sequence = [requested]
    current = requested if requested > 0 else 32
    while current > 0:
        current //= 2
        sequence.append(current)
    if sequence[-1] != 0:
        sequence.append(0)
    # Preserve order, drop duplicates.
    seen, ordered = set(), []
    for value in sequence:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def effective_gpu_layers():
    """The GPU layer count the currently loaded model was built with, if any."""
    return _effective_gpu_layers


def _get_model():
    global _model, _model_settings, _effective_gpu_layers
    settings = (
        config.LLM_MODEL,
        config.LLM_CONTEXT_SIZE,
        config.LLM_GPU_LAYERS,
        config.LLM_GPU_AUTO,
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
        requested = config.LLM_GPU_LAYERS
        if config.LLM_GPU_AUTO:
            requested = suggest_gpu_layers()[0]
        # Only an automatic choice is allowed to step itself down on failure; a
        # manual value is honoured exactly and surfaces its error to the operator.
        attempts = _gpu_fallback_sequence(requested) if config.LLM_GPU_AUTO else [requested]
        last_error = None
        for layers in attempts:
            options = dict(
                model_path=config.LLM_MODEL,
                n_ctx=config.LLM_CONTEXT_SIZE,
                n_gpu_layers=layers,
                n_threads=config.LLM_THREADS,
                n_threads_batch=config.LLM_THREADS,
                n_batch=config.LLM_BATCH_SIZE,
                use_mmap=True,
                verbose=False,
            )
            if config.LLM_CHAT_FORMAT:
                options["chat_format"] = config.LLM_CHAT_FORMAT
            try:
                _model = _load_llama_class()(**options)
                # DiskCache's pickle-backed persistence is deliberately disabled.
                _model.set_cache(None)
                _model_settings = settings
                _effective_gpu_layers = layers
                return _model
            except Exception as exc:  # noqa: BLE001 - retry with less GPU offload
                last_error = exc
                _model = None
        raise RuntimeError(
            f"The local model could not be loaded even on CPU: {last_error}"
        )


def _unload_local_model():
    """Release the process-local model instance without persisting any state."""
    global _model, _model_settings
    with _model_lock:
        if _model is not None:
            _model.close()
        _model = None
        _model_settings = None


def reload_model():
    """Reload with current loader settings, but only if a model is loaded now."""
    state = model_state()
    if state["status"] != "loaded" or not state["model_path"]:
        return False
    unload_model()
    load_model(state["model_path"])
    return True


def count_tokens(text):
    """Return the model's token count for a piece of text (for budgeting)."""
    return _request_model("count", {"text": text})


def _prompt_tokens(model, messages):
    formatter = _model_formatter(model)
    if formatter is not None:
        prompt = formatter(messages=messages).prompt
        return len(model.tokenize(prompt.encode("utf-8"), add_bos=False, special=True))
    prompt = "\n".join(message["content"] for message in messages)
    return len(model.tokenize(prompt.encode("utf-8", "ignore"))) + 256


def _model_formatter(model):
    if config.LLM_CHAT_FORMAT:
        return None
    template = model.metadata.get("tokenizer.chat_template", "")
    if not template:
        return None
    cached = getattr(model, "_operator_formatter", None)
    if cached is not None:
        return cached
    from llama_cpp.llama_chat_format import Jinja2ChatFormatter
    eos_id, bos_id = model.token_eos(), model.token_bos()
    if "enable_thinking" in template:
        template = "{% set enable_thinking = false %}" + template
    formatter = Jinja2ChatFormatter(
        template=template,
        eos_token=model.detokenize([eos_id], special=True).decode("utf-8") if eos_id >= 0 else "",
        bos_token=model.detokenize([bos_id], special=True).decode("utf-8") if bos_id >= 0 else "",
        stop_token_ids=[eos_id] if eos_id >= 0 else [],
    )
    model._operator_formatter = formatter
    return formatter


def _fit_messages(model, messages, max_tokens):
    """Keep the prompt within both the context window and the operator's cap.

    The limit is the smaller of (a) what the model's context window allows after
    reserving space for generation and (b) the operator's working-context cap
    (``MAX_CONTEXT_TOKENS``). When the prompt is too long, the last user message
    is trimmed middle-out so the task framing (its head) and the most recent
    evidence (its tail) are preserved. This keeps long sessions and large tool
    output from failing a run; the model simply sees a bounded window.
    """
    window = config.LLM_CONTEXT_SIZE - max_tokens - 64
    limit = min(window, config.MAX_CONTEXT_TOKENS)
    if window < 32:
        raise ValueError(
            "Context window is too small for the requested generation; "
            "lower max_tokens or raise llm.context_size."
        )
    if _prompt_tokens(model, messages) <= limit:
        return messages
    index = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        len(messages) - 1,
    )
    content = messages[index]["content"]
    # The objective and operator scope are never expendable context.
    prefix, separator, evidence = content.partition("UNTRUSTED PREVIOUS OBSERVATIONS:\n")
    if not separator:
        prefix, separator, evidence = content.partition("SESSION TRANSCRIPT:\n")
    protected = prefix + separator if separator else content
    content = evidence if separator else ""
    marker = "\n-- controller note: older context removed to fit the model window --\n"

    def shaped(keep_chars):
        if keep_chars >= len(content):
            return protected + content
        head_len = keep_chars * 20 // 100
        head = content[:head_len]
        tail = content[len(content) - (keep_chars - head_len):]
        return protected + head + marker + tail

    keep = len(content)
    while keep > 0:
        candidate = list(messages)
        candidate[index] = {**messages[index], "content": shaped(keep)}
        if _prompt_tokens(model, candidate) <= limit:
            return candidate
        keep = int(keep * 0.75) - 1
    minimal = list(messages)
    minimal[index] = {**messages[index], "content": protected}
    if _prompt_tokens(model, minimal) <= limit:
        return minimal
    raise ValueError("The goal and instructions do not fit this context with the requested response budget. Increase context or shorten the request.")


def _create_local_completion(messages, max_tokens, temperature, top_p, response_format=None, stream=False):
    # llama.cpp state is shared; tokenization and generation must be serialized.
    with _model_lock:
        model = _get_model()
        messages = _fit_messages(model, messages, max_tokens)
        options = dict(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        if stream:
            options["stream"] = True
        if response_format is not None:
            options["response_format"] = response_format
        # Some GGUF templates open a thinking block by default. A JSON grammar
        # then constrains reasoning rather than the final answer. Use the
        # template's own capability switch, without guessing from model names.
        formatter = _model_formatter(model)
        if formatter is not None:
            # Count the actual rendered template, including special tokens.
            prompt = formatter(messages=messages).prompt
            used = len(model.tokenize(prompt.encode("utf-8"), add_bos=False, special=True))
            available = config.LLM_CONTEXT_SIZE - used - 8
            if available < max_tokens:
                raise ValueError("Rendered prompt leaves insufficient room for a complete response")
            return formatter.to_chat_handler()(llama=model, **options)
        return model.create_chat_completion(**options)


def _model_worker(connection):
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            operation, settings, arguments = request
            for name, value in settings.items():
                setattr(config, name, value)
            try:
                if operation == "load":
                    _get_model()
                    value = None
                elif operation == "count":
                    value = len(_get_model().tokenize(str(arguments["text"]).encode("utf-8")))
                elif operation == "stream":
                    # Forward each generated fragment as it is produced.
                    finish = None
                    for chunk in _create_local_completion(**arguments, stream=True):
                        choice = (chunk.get("choices") or [{}])[0]
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            connection.send({"partial": text})
                        finish = choice.get("finish_reason") or finish
                    value = {"finish_reason": finish}
                else:
                    value = _create_local_completion(**arguments)
                connection.send({"ok": True, "value": (value, _effective_gpu_layers)})
            except Exception as exc:
                connection.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        _unload_local_model()
        connection.close()


_runtime = InferenceProcess(_model_worker)
atexit.register(_runtime.close)

# The operator explicitly chooses when a model occupies memory. The server
# starts with nothing loaded; runs and log questions require a loaded model.
_lifecycle_lock = threading.Lock()
_lifecycle = {
    "status": "unloaded",   # unloaded | loading | loaded | unloading | error
    "model_path": None,
    "error": None,
    "changed_at": None,
    "loaded_at": None,
    "notice": None,
}


class ModelNotLoadedError(RuntimeError):
    """Raised when inference is requested before the operator loads a model."""


def _set_lifecycle(**values):
    import time as _time
    with _lifecycle_lock:
        _lifecycle.update(values, changed_at=_time.time())


def model_state():
    """Operator-facing snapshot of the model lifecycle (no weights are touched)."""
    with _lifecycle_lock:
        state = dict(_lifecycle)
    # A cancelled generation tears down the inference process; reflect that.
    if state["status"] == "loaded" and not _runtime.is_alive():
        _set_lifecycle(status="unloaded", loaded_at=None,
                       notice="The model process ended (a run was stopped mid-generation). Load it again to continue.")
        with _lifecycle_lock:
            state = dict(_lifecycle)
    path = state.get("model_path")
    state["model_file"] = Path(path).name if path else None
    state["gpu_layers"] = _effective_gpu_layers if state["status"] == "loaded" else None
    return state


def is_loaded():
    return model_state()["status"] == "loaded"


def begin_load(model_path):
    """Atomically claim the lifecycle for a load; False if another operation owns it."""
    import time as _time
    with _lifecycle_lock:
        if _lifecycle["status"] in {"loading", "unloading"}:
            return False
        _lifecycle.update(status="loading", model_path=str(model_path), error=None, notice=None,
                          loaded_at=None, changed_at=_time.time())
    return True


def load_model(model_path, claimed=False):
    """Load ``model_path`` in the inference process. Blocks until loaded or failed."""
    if not claimed and not begin_load(model_path):
        raise RuntimeError("Another model operation is in progress")
    try:
        validate_runtime(model_path)
        _runtime.close()
        config.LLM_MODEL = str(model_path)
        _request_model("load", {}, require_loaded=False)
    except Exception as exc:
        _runtime.close()
        _set_lifecycle(status="error", error=str(exc)[:600])
        raise
    import time as _time
    _set_lifecycle(status="loaded", loaded_at=_time.time())


def load_model_async(model_path, on_done=None):
    """Load in the background; the caller must have claimed the lifecycle with begin_load()."""
    def work():
        try:
            load_model(model_path, claimed=True)
        except Exception as exc:  # noqa: BLE001 - surfaced through model_state()
            print(f"Model load failed ({type(exc).__name__}): {exc}")
        finally:
            if on_done:
                on_done()
    threading.Thread(target=work, daemon=True, name="hephestus-model-load").start()


def unload_model():
    global _effective_gpu_layers
    with _lifecycle_lock:
        had_model = _lifecycle["status"] in {"loaded", "error", "loading"}
    if had_model:
        _set_lifecycle(status="unloading")
    _runtime.close()
    _effective_gpu_layers = None
    _set_lifecycle(status="unloaded", loaded_at=None, error=None)


def _request_model(operation, arguments, cancel_event=None, require_loaded=True, on_partial=None):
    global _effective_gpu_layers
    if require_loaded and not is_loaded():
        raise ModelNotLoadedError("No model is loaded. Open Models and load one first.")
    settings = {name: getattr(config, name) for name in config._SETTINGS}
    value, _effective_gpu_layers = _runtime.request((operation, settings, arguments), cancel_event, on_partial)
    return value


def stream_chat_completion(messages, max_tokens, temperature, top_p, on_text):
    """Generate with incremental text delivered to ``on_text``; returns the finish reason."""
    result = _request_model("stream", dict(messages=messages, max_tokens=max_tokens,
                            temperature=temperature, top_p=top_p), on_partial=on_text)
    return (result or {}).get("finish_reason")


def create_chat_completion(messages, max_tokens, temperature, top_p, response_format=None, cancel_event=None):
    return _request_model("complete", dict(messages=messages, max_tokens=max_tokens,
        temperature=temperature, top_p=top_p, response_format=response_format), cancel_event)

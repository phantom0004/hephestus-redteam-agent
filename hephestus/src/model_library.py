"""The project's local GGUF library: discovery, metadata, cover art and removal.

Only files inside the project ``models`` folder are ever listed, served or
deleted. Identifiers are paths relative to that folder and are always
re-resolved against a fresh scan, so a client cannot address anything outside it.
"""

import hashlib
import re
import threading
from pathlib import Path

from hephestus import config
from hephestus.src import local_llm

MODELS_DIR = config.DATA_ROOT / "models"
COVERS_DIR = MODELS_DIR / "covers"
COVER_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_COVER_BYTES = 3 * 1024 * 1024
_metadata_cache = {}
_cache_lock = threading.Lock()


def models_dir():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def _metadata(path):
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _cache_lock:
        if key in _metadata_cache:
            return _metadata_cache[key]
    meta = local_llm.read_model_metadata(path)
    with _cache_lock:
        _metadata_cache[key] = meta
    return meta


def _quant_from_name(name):
    match = re.search(r"(?<![A-Za-z0-9])(I?Q\d(?:_K)?(?:_[A-Z0-9]{1,3})?|BF16|F16|F32)(?![A-Za-z0-9])", name, re.I)
    return match.group(1).upper() if match else ""


def _model_id(path):
    return path.relative_to(models_dir()).as_posix()


def _accent(model_id):
    """A stable hue per model, used for generated cartridge art."""
    return int(hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:4], 16) % 360


def cover_path(path):
    for directory in (COVERS_DIR, path.parent):
        for extension in COVER_TYPES:
            candidate = directory / (path.stem + extension)
            if candidate.is_file():
                return candidate
    return None


def scan():
    """Every .gguf under the models folder with lightweight header metadata."""
    root = models_dir()
    state = local_llm.model_state()
    active = state.get("model_path")
    active = str(Path(active).resolve()) if active else None
    models = []
    for path in sorted(root.rglob("*.gguf"), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.endswith(".part"):
            continue
        model_id = _model_id(path)
        meta = _metadata(path)
        resolved = str(path.resolve())
        status = state["status"] if resolved == active else "unloaded"
        models.append({
            "id": model_id,
            "file": path.name,
            "name": meta.get("name") or path.stem,
            "size_bytes": path.stat().st_size,
            "arch": meta.get("arch") or "",
            "quantization": meta.get("quantization") or _quant_from_name(path.name),
            "size_label": meta.get("size_label") or "",
            "layers": meta.get("n_layers"),
            "context_length": meta.get("context_length"),
            "valid_gguf": bool(meta),
            "has_cover": cover_path(path) is not None,
            "accent": _accent(model_id),
            "status": status,
            "is_default": resolved == str(Path(config.LLM_MODEL).resolve()),
        })
    return models


def resolve(model_id):
    """Map an identifier from the UI back to a file that a fresh scan still lists."""
    if not isinstance(model_id, str) or not model_id or len(model_id) > 400:
        raise ValueError("Unknown model")
    root = models_dir().resolve()
    for path in root.rglob("*.gguf"):
        if _model_id(path) == model_id and path.is_file():
            resolved = path.resolve()
            if root not in resolved.parents:
                break
            return resolved
    raise FileNotFoundError("Model not found in the project models folder")


def _sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_cover(model_id, data):
    path = resolve(model_id)
    if not data or len(data) > MAX_COVER_BYTES:
        raise ValueError("Cover images must be at most 3 MB")
    extension = _sniff_image(data)
    if extension is None:
        raise ValueError("Cover must be a PNG, JPEG or WebP image")
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    for existing in COVER_TYPES:
        (COVERS_DIR / (path.stem + existing)).unlink(missing_ok=True)
    target = COVERS_DIR / (path.stem + extension)
    temporary = target.with_suffix(extension + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return target


def remove_cover(model_id):
    path = resolve(model_id)
    for extension in COVER_TYPES:
        (COVERS_DIR / (path.stem + extension)).unlink(missing_ok=True)


def delete_model(model_id):
    """Permanently delete one model file (and its cover) from the project folder."""
    path = resolve(model_id)
    state = local_llm.model_state()
    active = state.get("model_path")
    if active and Path(active).resolve() == path and state["status"] in {"loaded", "loading", "unloading"}:
        raise PermissionError("Unload this model before deleting it")
    path.unlink()
    for extension in COVER_TYPES:
        (COVERS_DIR / (path.stem + extension)).unlink(missing_ok=True)
    return path.name

"""One JSON file per session in the project ``sessions`` folder.

Sessions survive a server restart so they can be reopened, questioned and
exported later. Deleting a session in the UI deletes its file. Writes are
atomic (temporary file + rename) so a crash never leaves a half-written file.
"""

import json
import os
import re
import time
from pathlib import Path

SCHEMA = "hephestus.session/v1"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SessionStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _path(self, run_id):
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run id")
        return self.directory / f"{run_id}.json"

    def save(self, record):
        path = self._path(record["run_id"])
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": SCHEMA, **record}, ensure_ascii=False, default=str)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return True
            except PermissionError:
                # Windows refuses to replace a file another reader has open.
                time.sleep(0.05 * (attempt + 1))
        temporary.unlink(missing_ok=True)
        return False

    def load(self, run_id):
        try:
            data = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("run_id") == run_id else None

    def delete(self, run_id):
        try:
            path = self._path(run_id)
        except ValueError:
            return False
        path.unlink(missing_ok=True)
        path.with_suffix(".json.tmp").unlink(missing_ok=True)
        return True

    def load_all(self):
        """Every readable saved session, newest first."""
        if not self.directory.is_dir():
            return []
        records = []
        for path in self.directory.glob("*.json"):
            if not _RUN_ID.fullmatch(path.stem):
                continue
            record = self.load(path.stem)
            if record is not None and isinstance(record.get("logs"), list):
                records.append(record)
        records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return records

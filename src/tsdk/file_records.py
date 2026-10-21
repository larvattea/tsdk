"""Registered-player records in one JSON file, for developer mode and any game.

File shape: {"configuration": {...}, "sessions": [{"id": 1, ...values, "events": [{...}]}]}.
Every change rewrites the file atomically, so a crash leaves the previous or the new version.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_SESSION_KEYS = frozenset({"id", "events"})  # owned by the records, not by the game


class FileRecords:
    """RegisteredGameRecords persisted to a JSON file; values must be JSON-compatible."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        """The whole file; empty configuration and sessions when it does not exist yet."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"configuration": {}, "sessions": []}
        if not isinstance(data, dict) or not isinstance(data.get("configuration", {}), dict) \
                or not isinstance(data.get("sessions", []), list):
            raise ValueError(f"{self.path}: not a player records file")
        return {"configuration": data.get("configuration", {}), "sessions": data.get("sessions", [])}

    def _save(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".records-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def configuration(self, defaults: dict) -> dict:
        """Saved values over defaults; the merge is saved, so the file lists every setting."""
        data = self.load()
        data["configuration"] = {**defaults, **data["configuration"]}
        self._save(data)
        return dict(data["configuration"])

    def update_configuration(self, values: dict) -> None:
        data = self.load()
        data["configuration"].update(values)
        self._save(data)

    def replace_configuration(self, values: dict) -> None:
        """Set the whole configuration, keeping sessions; settings left out fall back to game defaults."""
        data = self.load()
        data["configuration"] = dict(values)
        self._save(data)

    def start_session(self, values: dict) -> int:
        self._check_session_values(values)
        data = self.load()
        session_id = max((session["id"] for session in data["sessions"]), default=0) + 1
        data["sessions"].append({"id": session_id, **values, "events": []})
        self._save(data)
        return session_id

    def update_session(self, session_id: int, values: dict) -> None:
        self._check_session_values(values)
        data = self.load()
        self._session(data, session_id).update(values)
        self._save(data)

    def record_event(self, session_id: int, values: dict) -> None:
        data = self.load()
        self._session(data, session_id)["events"].append(dict(values))
        self._save(data)

    @staticmethod
    def _check_session_values(values: dict) -> None:
        reserved = _SESSION_KEYS & set(values)
        if reserved:
            raise ValueError(f"Session values may not set {sorted(reserved)}")

    @staticmethod
    def _session(data: dict, session_id: int) -> dict:
        for session in data["sessions"]:
            if session["id"] == session_id:
                return session
        raise KeyError(f"Unknown session {session_id}")

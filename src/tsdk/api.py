"""Version-one game lifecycle and game-scoped persistent JSON storage."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from platformdirs import user_data_dir

if TYPE_CHECKING:
    import pygame
    from .tracking import TrackingFrame


def _component(value: str) -> str:
    if not value or value in {".", ".."} or any(c in value for c in '/\\\x00'):
        raise ValueError("Storage names must be single path components")
    return value


class SessionStorage:
    """JSON saves and immutable result records, independent of installed code."""

    def __init__(self, game_id: str, player_id: str = "anonymous", root: Path | None = None) -> None:
        base = Path(root) if root is not None else Path(user_data_dir("ttea", "udesc")) / "tsdk" / "sessions"
        self.root = base / _component(game_id) / _component(str(player_id))
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, name: str, default: Any = None) -> Any:
        path = self.root / (_component(name) + ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def write(self, name: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        destination = self.root / (_component(name) + ".json")
        descriptor, temporary = tempfile.mkstemp(prefix=".save-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def record_result(self, result: dict) -> Path:
        name = "result-" + uuid.uuid4().hex
        self.write(name, {"recorded_at": datetime.now(timezone.utc).isoformat(), "result": result})
        return self.root / (name + ".json")


class RegisteredGameRecords(Protocol):
    """Platform-owned registered-player records; values are JSON-compatible.

    The platform binds identity and game. Consumers never receive patient
    models, filesystem locations, DAOs, or arbitrary-player access.
    """

    def configuration(self, defaults: dict) -> dict:
        """Read saved configuration, creating it from defaults if absent."""
        ...

    def update_configuration(self, values: dict) -> None:
        ...

    def start_session(self, values: dict) -> int:
        ...

    def update_session(self, session_id: int, values: dict) -> None:
        ...

    def record_event(self, session_id: int, values: dict) -> None:
        ...


@dataclass
class GameContext:
    surface: pygame.Surface
    tracking: TrackingFrame
    dt: float
    elapsed: float
    events: tuple[pygame.event.Event, ...]
    assets: Path
    config: dict
    storage: SessionStorage
    paused: bool = False
    pause_reason: str | None = None
    stop_requested: bool = False
    result: dict | None = field(default=None, repr=False)
    records: RegisteredGameRecords | None = field(default=None, repr=False)

    def request_stop(self, result: dict | None = None) -> None:
        self.stop_requested = True
        if result is not None:
            self.result = result


class Game:
    """Subclass and override callbacks; the platform owns the event loop."""

    def start(self, context: GameContext) -> None:
        pass

    def update(self, context: GameContext) -> None:
        pass

    def draw(self, context: GameContext) -> None:
        pass

    def pause(self, context: GameContext) -> None:
        pass

    def resume(self, context: GameContext) -> None:
        pass

    def stop(self, context: GameContext) -> dict | None:
        return None


DWELL_SECONDS = 2.0     # stay on a button this long to choose it
LEAVE_TOLERANCE = 0.5   # seconds off the button that still keep the count
SELECT_MARGIN = 30      # px the button being chosen grows on every side (hysteresis)


def feet_centre(tracking: TrackingFrame | None) -> tuple[float, float] | None:
    """Game-normalized midpoint of both feet (0..1, x right, y down), or None when tracking is
    missing or stale, either foot is absent, or either foot is out of area."""
    if tracking is None or tracking.missing or tracking.stale:
        return None
    left, right = tracking.feet.get("left"), tracking.feet.get("right")
    if left is None or right is None or tracking.out_of_area.get("left") or tracking.out_of_area.get("right"):
        return None
    return (left.x + right.x) / 2, (left.y + right.y) / 2


class FloorSelector:
    """Floor buttons chosen by standing on them for DWELL_SECONDS.

    Hysteresis: tracking jitters a few pixels per frame, so the button being selected grows
    SELECT_MARGIN on every side, and leaving it for at most LEAVE_TOLERANCE seconds keeps the count.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.selecting: str | None = None
        self.held = self.away = 0.0

    @property
    def progress(self) -> dict[str, float]:
        """Fill fraction of the button being selected."""
        return {} if self.selecting is None else {self.selecting: min(self.held / DWELL_SECONDS, 1.0)}

    def update(self, cursor: tuple[float, float] | None, buttons: dict[str, pygame.Rect], dt: float) -> str | None:
        """Advance by dt among the enabled `buttons` (cursor in their pixel space); returns a name once chosen."""
        def on(rect: pygame.Rect) -> bool:
            return cursor is not None and bool(rect.collidepoint(cursor))

        current = self.selecting if self.selecting in buttons else None
        if current is not None and on(buttons[current].inflate(2 * SELECT_MARGIN, 2 * SELECT_MARGIN)):
            self.away = 0.0
        else:
            entered = next((name for name, rect in buttons.items() if name != current and on(rect)), None)
            if entered is not None:
                self.selecting, self.held, self.away = entered, 0.0, 0.0
                return None
            if current is None:
                self.reset()
                return None
            self.away += dt
            if self.away > LEAVE_TOLERANCE:
                self.reset()
                return None
        self.held += dt
        if self.held < DWELL_SECONDS:
            return None
        self.reset()
        return current

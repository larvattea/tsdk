"""`tsdk play`: run a cartridge for the TTEA console, configured from TTEA's own calibration files.

TTEA writes `<data-dir>/calibration/` with Qt's INI format and a semicolon CSV; tsdk only reads them.
Upstream records neither the frame size the corners were picked in nor whether that frame was
mirrored. Its calibration screens open the camera without a size request, so the points are pixels
of the camera's default frame; tsdk opens the camera the same way and measures that size. Like
TTEA's own runtime (KarTEA), it reads them as the projected screen's corners top-left, top-right,
bottom-left, bottom-right of the horizontally mirrored frame, and fills that screen with the game.
"""
from __future__ import annotations

import configparser
import csv
from pathlib import Path
import tempfile

from .api import _component
from .cartridge import extract_cartridge
from .providers import default_frame_size

HARDWARE_FILE = "calibration_hardware.ini"
POINTS_FILE = "calibration_point.csv"
CORNERS = ((0, 0), (1, 0), (0, 1), (1, 1))  # game-normalized targets of points 1 to 4


def _calibration_file(data_dir: Path, name: str) -> Path:
    path = Path(data_dir) / "calibration" / name
    if not path.is_file():
        raise ValueError(f"Missing TTEA calibration {path}; calibrate in TTEA before playing")
    return path


def read_hardware(path: Path) -> dict:
    """Camera index plus the projector's screen index and size from calibration_hardware.ini."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as error:
        raise ValueError(f"{path}: not a TTEA hardware calibration file") from error
    if not parser.get("camera", "camera_id", fallback="").strip():
        raise ValueError(f"{path}: no camera saved; save the hardware configuration in TTEA")
    values = {}
    for section, key in (("camera", "camera_position"), ("screen", "screen_position"),
                         ("screen", "screen_width"), ("screen", "screen_height")):
        try:
            values[key] = parser.getint(section, key)
        except (configparser.Error, ValueError) as error:
            raise ValueError(f"{path}: invalid {section}/{key}") from error
    if min(values["screen_width"], values["screen_height"]) <= 0:
        raise ValueError(f"{path}: screen size must be positive; save the hardware configuration in TTEA")
    return values


def read_points(path: Path) -> list[tuple[int, int]]:
    """The four calibrated corners from calibration_point.csv."""
    with Path(path).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter=";", skipinitialspace=True))
    if not rows:
        raise ValueError(f"{path}: no calibration points; calibrate in TTEA before playing")
    try:
        return [(int(rows[0][f"pointx{i}"]), int(rows[0][f"pointy{i}"])) for i in range(1, 5)]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{path}: invalid calibration points") from error


def homography(points: list[tuple[int, int]], width: int, height: int) -> list[list[float]]:
    """Map raw-camera normalized coordinates to game-normalized ones (top-left origin)."""
    import cv2
    import numpy as np

    source = np.array(points, dtype=np.float32)
    source[:, 0] = width - 1 - source[:, 0]
    source /= (width, height)
    matrix = cv2.getPerspectiveTransform(source, np.array(CORNERS, dtype=np.float32))
    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12:
        raise ValueError("TTEA calibration points are degenerate; recalibrate in TTEA")
    return matrix.tolist()


def console_config(data_dir: Path, language: str, player_id: str, professional_id: str, game_id: str) -> dict:
    """The runtime configuration for one console session; game data lives under <data-dir>/tsdk/<game_id>/."""
    data_dir = Path(data_dir)
    hardware = read_hardware(_calibration_file(data_dir, HARDWARE_FILE))
    points = read_points(_calibration_file(data_dir, POINTS_FILE))
    width, height = default_frame_size(hardware["camera_position"])
    games = data_dir / "tsdk"
    return {
        "hardware": {"camera_index": hardware["camera_position"]},
        "calibration": {"homography": homography(points, width, height)},
        "width": hardware["screen_width"],
        "height": hardware["screen_height"],
        "display": hardware["screen_position"],
        "language": language,
        "player_id": player_id,
        "professional_id": professional_id,
        "session_root": str(games),
        "records": str(games / _component(game_id) / "records" / (_component(player_id) + ".json")),
    }


def play(cartridge: Path, language: str, player_id: str, professional_id: str, data_dir: Path) -> dict:
    """Validate and extract the cartridge privately, then run one session with the camera."""
    from .runtime import run_game

    with tempfile.TemporaryDirectory(prefix="tsdk-play-") as temporary:
        project = Path(temporary) / "game"
        manifest = extract_cartridge(Path(cartridge).resolve(strict=True), project)
        config = console_config(data_dir, language, player_id, professional_id, manifest["id"])
        return run_game(project, manifest, config=config)

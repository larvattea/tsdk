"""Camera-normalized observations and normalized planar projection.

World landmarks remain model-relative 3D, not room coordinates. Projection does
not establish floor contact. Unsupported visibility is represented by None.
"""

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import time


PROFILES = {"body", "hands", "face"}


@dataclass
class Landmark:
    x: float
    y: float
    z: float | None = None
    visibility: float | None = None


@dataclass
class Observation:
    """Provider-neutral data; confidence_source is measured, authored, or unknown.

    Handedness maps each anatomical side to {label, score}; absent scores are None.
    World coordinates use meters relative to the model, never room coordinates.
    """
    profile: str
    timestamp: float
    landmarks: dict[str, list[Landmark]] = field(default_factory=dict)
    world_landmarks: dict[str, list[Landmark]] = field(default_factory=dict)
    handedness: dict = field(default_factory=dict)
    expressions: dict = field(default_factory=dict)
    transformation_matrix: list | None = None
    present: bool = True
    confidence_source: str | None = None


@dataclass
class TrackingFrame:
    observation: Observation
    projection: dict[str, list[Landmark]]
    feet: dict[str, Landmark]
    missing: bool
    stale: bool
    out_of_area: dict


def observation_to_dict(observation: Observation) -> dict:
    return asdict(observation)


def observation_from_dict(data: dict) -> Observation:
    values = dict(data)
    for key in ("landmarks", "world_landmarks"):
        values[key] = {
            name: [Landmark(**point) for point in points]
            for name, points in values.get(key, {}).items()
        }
    return Observation(**values)


class TrackingPipeline:
    def __init__(self, profile, calibration=None, config=None):
        if profile not in PROFILES:
            raise ValueError(f"Unknown tracking profile: {profile}")
        self.profile = profile
        self.calibration = calibration
        self.config = dict(config or {})
        matrix = calibration.get("homography") if isinstance(calibration, dict) else calibration
        self.matrix = matrix if matrix is not None else ((1, 0, 0), (0, 1, 0), (0, 0, 1))
        if len(self.matrix) != 3 or any(len(row) != 3 for row in self.matrix):
            raise ValueError("Calibration homography must be 3 by 3")
        if not all(math.isfinite(float(value)) for row in self.matrix for value in row):
            raise ValueError("Calibration homography must be finite")
        self.stale_after = float(self.config.get("stale_after", 0.5))
        self.min_visibility = float(self.config.get("min_visibility", 0.0))
        if self.stale_after < 0 or not math.isfinite(self.stale_after):
            raise ValueError("stale_after must be finite and nonnegative")

    def _usable(self, point):
        return (math.isfinite(point.x) and math.isfinite(point.y)
                and (point.visibility is None or point.visibility >= self.min_visibility))

    def _project(self, point):
        a, b, c = self.matrix
        denominator = c[0] * point.x + c[1] * point.y + c[2]
        if not self._usable(point) or abs(denominator) < 1e-12:
            return Landmark(math.nan, math.nan, visibility=point.visibility)
        return Landmark(
            (a[0] * point.x + a[1] * point.y + a[2]) / denominator,
            (b[0] * point.x + b[1] * point.y + b[2]) / denominator,
            visibility=point.visibility,
        )

    @staticmethod
    def _outside(point):
        return not (0 <= point.x <= 1 and 0 <= point.y <= 1)

    def process(self, observation, now=None):
        if observation.profile != self.profile:
            raise ValueError("Observation profile differs from pipeline profile")
        now = time.monotonic() if now is None else now
        stale = not math.isfinite(observation.timestamp) or now - observation.timestamp > self.stale_after
        missing = not observation.present or not any(
            self._usable(point) for points in observation.landmarks.values() for point in points
        )
        projection = {}
        feet = {}
        outside = {}
        if not missing and not stale:
            projection = {key: [self._project(point) for point in points]
                          for key, points in observation.landmarks.items()}
            outside = {key: [self._outside(point) for point in points]
                       for key, points in projection.items()}
            body = projection.get("body", [])
            if len(body) >= 33:
                for side, heel_index, toe_index in (("left", 29, 31), ("right", 30, 32)):
                    heel, toe = body[heel_index], body[toe_index]
                    for name, point in ((f"{side}_heel", heel), (f"{side}_toe", toe)):
                        if self._usable(point):
                            feet[name] = point
                    if self._usable(heel) and self._usable(toe):
                        confidence = (min(heel.visibility, toe.visibility)
                                      if heel.visibility is not None and toe.visibility is not None else None)
                        feet[side] = Landmark((heel.x + toe.x) / 2, (heel.y + toe.y) / 2,
                                              visibility=confidence)
            outside.update({name: self._outside(point) for name, point in feet.items()})
        return TrackingFrame(observation, projection, feet, missing, stale, outside)


class Recording:
    """Streaming JSONL recording without images or video."""

    def __init__(self, path, profile, calibration=None, config=None):
        if profile not in PROFILES:
            raise ValueError(f"Unknown tracking profile: {profile}")
        self.profile = profile
        self._file = Path(path).open("w", encoding="utf-8")
        try:
            self._file.write(json.dumps({"format_version": 1, "profile": profile,
                                         "calibration": calibration, "config": config or {}},
                                        allow_nan=False) + "\n")
        except Exception:
            self._file.close()
            raise

    def write(self, observation, now=None):
        if observation.profile != self.profile:
            raise ValueError("Observation profile differs from recording profile")
        record = {"observation": observation_to_dict(observation),
                  "now": time.monotonic() if now is None else now}
        self._file.write(json.dumps(record, allow_nan=False) + "\n")
        self._file.flush()

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ReplayProvider:
    def __init__(self, path):
        self._file = Path(path).open(encoding="utf-8")
        try:
            header = json.loads(next(self._file))
            if header.get("format_version") != 1 or header.get("profile") not in PROFILES:
                raise ValueError("Unsupported tracking recording")
            self.profile = header["profile"]
            self.calibration = header.get("calibration")
            self.config = header.get("config", {})
            self.now = None
        except Exception:
            self._file.close()
            raise

    def read(self):
        record = json.loads(next(self._file))
        observation = observation_from_dict(record["observation"])
        self.now = record["now"]
        if observation.profile != self.profile:
            raise ValueError("Recording contains a different tracking profile")
        return observation

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

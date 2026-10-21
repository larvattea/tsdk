"""Runtime-owned camera and MediaPipe Tasks detectors, one profile per session."""

from pathlib import Path
import sys
import time

from .tracking import Landmark, Observation, PROFILES

MODELS = Path(__file__).with_name("models")

# OpenCV defines every backend constant on every platform, so choose by OS.
CAPTURE_BACKENDS = {"darwin": "CAP_AVFOUNDATION", "win32": "CAP_DSHOW", "linux": "CAP_V4L2"}


def _open_camera(cv2, camera_index):
    backend = CAPTURE_BACKENDS.get("linux" if sys.platform.startswith("linux") else sys.platform)
    return cv2.VideoCapture(camera_index, getattr(cv2, backend)) if backend else cv2.VideoCapture(camera_index)


def default_frame_size(camera_index):
    """Width and height of the frames the camera delivers when opened without a size request."""
    import cv2

    camera = _open_camera(cv2, camera_index)
    try:
        ok, frame = camera.read() if camera.isOpened() else (False, None)
        if not ok:
            raise RuntimeError(f"Unable to read camera {camera_index}")
        height, width = frame.shape[:2]
        return width, height
    finally:
        camera.release()


class HardwareProvider:
    def __init__(self, profile, camera_index=0, model_path=None, width=None, height=None,
                 min_detection_confidence=0.5, min_tracking_confidence=0.5):
        if profile not in PROFILES:
            raise ValueError(f"Unknown tracking profile: {profile}")
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        self.profile = profile
        self._cv2 = cv2
        self._mp = mp
        self._camera = None
        self.frame = None
        self._detector = None
        self._last_timestamp_ms = -1
        filenames = {"body": "pose_landmarker_full.task", "hands": "hand_landmarker.task",
                     "face": "face_landmarker.task"}
        base = python.BaseOptions(model_asset_path=str(model_path or MODELS / filenames[profile]))
        common = {"base_options": base, "running_mode": vision.RunningMode.VIDEO,
                  "min_tracking_confidence": min_tracking_confidence}
        try:
            if profile == "body":
                options = vision.PoseLandmarkerOptions(
                    **common, num_poses=1, min_pose_detection_confidence=min_detection_confidence)
                self._detector = vision.PoseLandmarker.create_from_options(options)
            elif profile == "hands":
                options = vision.HandLandmarkerOptions(
                    **common, num_hands=2, min_hand_detection_confidence=min_detection_confidence)
                self._detector = vision.HandLandmarker.create_from_options(options)
            else:
                options = vision.FaceLandmarkerOptions(
                    **common, num_faces=1, min_face_detection_confidence=min_detection_confidence,
                    output_face_blendshapes=True, output_facial_transformation_matrixes=True)
                self._detector = vision.FaceLandmarker.create_from_options(options)
            self._camera = _open_camera(cv2, camera_index)
            if not self._camera.isOpened():
                raise RuntimeError(f"Unable to open camera {camera_index}")
            if width is not None:
                self._camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            if height is not None:
                self._camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _points(points, visibility=False):
        return [Landmark(point.x, point.y, point.z,
                         getattr(point, "visibility", None) if visibility else None)
                for point in points]

    def read(self):
        if self._camera is None:
            raise RuntimeError("Tracking provider is closed")
        ok, frame = self._camera.read()
        timestamp = time.monotonic()
        if not ok:
            self.frame = None
            return Observation(self.profile, timestamp, present=False)
        self.frame = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=self.frame)
        timestamp_ms = max(int(timestamp * 1000), self._last_timestamp_ms + 1)
        self._last_timestamp_ms = timestamp_ms
        result = self._detector.detect_for_video(image, timestamp_ms)
        return self._observation(result, timestamp)

    def _observation(self, result, timestamp):
        observation = Observation(self.profile, timestamp, present=False, confidence_source="measured")
        if self.profile == "body":
            if result.pose_landmarks:
                observation.landmarks["body"] = self._points(result.pose_landmarks[0], visibility=True)
                if result.pose_world_landmarks:
                    observation.world_landmarks["body"] = self._points(result.pose_world_landmarks[0], visibility=True)
        elif self.profile == "hands":
            for index, points in enumerate(result.hand_landmarks):
                categories = result.handedness[index]
                if not categories:
                    continue
                category = max(categories, key=lambda item: item.score)
                # Raw (unmirrored) frames: Tasks labels are the person's anatomical hand. The legacy
                # Solutions note about mirrored input does not hold for 0.10.21's hand_landmarker.task:
                # its right_hands.jpg/thumb_up.jpg testdata expect "Right" unflipped, flipping yields
                # "Left", and world-landmark palm geometry agrees. Do not swap.
                side = category.category_name.lower()
                if side not in ("left", "right"):
                    continue
                previous = observation.handedness.get(side)
                if previous is not None and previous["score"] >= category.score:
                    continue
                observation.landmarks[side] = self._points(points)
                observation.handedness[side] = {"label": side, "score": category.score}
                observation.world_landmarks.pop(side, None)
                if index < len(result.hand_world_landmarks):
                    observation.world_landmarks[side] = self._points(result.hand_world_landmarks[index])
        else:
            if result.face_landmarks:
                observation.landmarks["face"] = self._points(result.face_landmarks[0])
                if result.face_blendshapes:
                    observation.expressions = {category.category_name: category.score
                                               for category in result.face_blendshapes[0]}
                if result.facial_transformation_matrixes:
                    observation.transformation_matrix = result.facial_transformation_matrixes[0].tolist()
        observation.present = bool(observation.landmarks)
        return observation

    def close(self):
        try:
            if self._camera is not None:
                self._camera.release()
        finally:
            self._camera = None
            if self._detector is not None:
                self._detector.close()
                self._detector = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

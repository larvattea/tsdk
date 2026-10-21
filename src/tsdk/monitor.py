"""Operator view for `tsdk play`: the camera, the calibrated area, and the detected skeleton.

Like TTEA's own runtime (KarTEA), the frame is mirrored, so the calibration corners (pixels of the
mirrored frame) and the player's moves line up with what the operator sees.
"""
from __future__ import annotations

from .simulator import BODY_EDGES

WIDTH = 960  # px: window width; the height follows the camera's aspect
AREA_COLOR = (34, 197, 94)
BONE_COLOR = (94, 234, 212)
JOINT_COLOR = (251, 191, 36)


class CameraMonitor:
    def __init__(self, title: str, points: list[tuple[int, int]]):
        import pygame

        self._pygame = pygame
        self._points = points
        self._window = None
        self._title = title

    def show(self, frame, observation) -> None:
        """Draw one RGB camera frame (raw, unmirrored) with the area and the observation's body."""
        if frame is None:
            return
        import cv2

        pygame = self._pygame
        height, width = frame.shape[:2]
        size = (WIDTH, round(height * WIDTH / width))
        if self._window is None:
            game = pygame.Window.from_display_module()
            self._window = pygame.Window(self._title, size)
            game.focus()  # the game keeps the keyboard (Esc, pause)
        image = cv2.flip(cv2.resize(frame, size, interpolation=cv2.INTER_AREA), 1)
        surface = self._window.get_surface()
        surface.blit(pygame.image.frombuffer(image.tobytes(), size, "RGB"), (0, 0))
        scale = size[0] / width
        top_left, top_right, bottom_left, bottom_right = [(x * scale, y * scale) for x, y in self._points]
        pygame.draw.polygon(surface, AREA_COLOR, (top_left, top_right, bottom_right, bottom_left), 2)
        body = observation.landmarks.get("body")
        if body:
            joints = [((1 - point.x) * size[0], point.y * size[1]) for point in body]
            for a, b in BODY_EDGES:
                pygame.draw.line(surface, BONE_COLOR, joints[a], joints[b], 3)
            for joint in joints[11:]:
                pygame.draw.circle(surface, JOINT_COLOR, joint, 4)
        self._window.flip()

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()
            self._window = None

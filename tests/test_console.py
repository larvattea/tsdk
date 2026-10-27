"""`tsdk play` reads TTEA's own calibration files, written here exactly as upstream TTEA writes them."""
import contextlib
import io
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

import numpy

from tsdk.cli import main
from tsdk.console import console_config
from tsdk.providers import default_frame_size

# QSettings IniFormat output of upstream CalibrationIniDAO: one section per field prefix, sorted.
HARDWARE = """[camera]
camera_description=FaceTime HD Camera
camera_height=720
camera_id=0x1420000005ac8600
camera_max_fps=30
camera_min_fps=1
camera_position=1
camera_width=1280

[content]
content_height=900
content_height_ratio=9
content_proportion=16:9
content_width=1600
content_width_ratio=16

[info]
calibration_date=21/10/2026
calibration_hour=10:30:00

[screen]
screen_available_height=1050
screen_available_width=1680
screen_height=1050
screen_manufacturer=Apple
screen_model=Color LCD
screen_pos_x=0
screen_pos_y=0
screen_position=0
screen_serial_number=
screen_width=1680
"""
# Upstream CalibrationPointCsvDAO: CSVHandler's ";" dialect, header from CalibrationPoint.PROPERTIES.
# Projected screen corners TL, TR, BL, BR in pixels of the camera's default (here 640x480) mirrored frame, as
# TTEA's calibration screens pick them; the ini's 1280x720 Qt camera format plays no part.
POINTS = "pointx1;pointy1;pointx2;pointy2;pointx3;pointy3;pointx4;pointy4\n50;40;590;40;50;440;590;440\n"


def project(matrix, x, y):
    a, b, c = matrix
    w = c[0] * x + c[1] * y + c[2]
    return (a[0] * x + a[1] * y + a[2]) / w, (b[0] * x + b[1] * y + b[2]) / w


class ConsoleCalibrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        (self.data / "calibration").mkdir()

    def test_upstream_files_give_camera_config_and_homography(self):
        (self.data / "calibration" / "calibration_hardware.ini").write_text(HARDWARE, encoding="utf-8")
        (self.data / "calibration" / "calibration_point.csv").write_text(POINTS, encoding="utf-8")
        with patch("tsdk.console.default_frame_size", return_value=(640, 480)) as frame_size:
            config = console_config(self.data, "pt_BR", "7", "3", "example.game")
        frame_size.assert_called_once_with(1)
        self.assertEqual(config["hardware"], {"camera_index": 1})
        self.assertEqual((config["width"], config["height"], config["display"]), (1680, 1050, 0))
        self.assertEqual((config["language"], config["player_id"], config["professional_id"]), ("pt_BR", "7", "3"))
        self.assertEqual(config["session_root"], str(self.data / "tsdk"))
        self.assertEqual(config["records"], str(self.data / "tsdk" / "example.game" / "records" / "7.json"))
        matrix = config["calibration"]["homography"]
        # Raw camera frames are unmirrored: mirrored pixel u is raw pixel 639 - u.
        for (u, v), corner in zip(((50, 40), (590, 40), (50, 440), (590, 440)), ((0, 0), (1, 0), (0, 1), (1, 1))):
            with self.subTest(corner=corner):
                x, y = project(matrix, (639 - u) / 640, v / 480)
                self.assertAlmostEqual(x, corner[0], places=5)
                self.assertAlmostEqual(y, corner[1], places=5)

    def test_missing_calibration_exits_nonzero_with_message(self):
        (self.data / "calibration" / "calibration_point.csv").write_text(POINTS, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "calibration_hardware.ini"):
            console_config(self.data, "en", "1", "1", "example.game")
        (self.data / "calibration" / "calibration_hardware.ini").write_text(
            HARDWARE.replace("camera_id=0x1420000005ac8600", "camera_id="), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no camera"):
            console_config(self.data, "en", "1", "1", "example.game")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["play", str(self.data / "missing.ttea"), "--lang", "en", "--player_id", "1",
                         "--professional_id", "1", "--data-dir", str(self.data)])
        self.assertEqual(code, 1)
        self.assertTrue(stderr.getvalue().startswith("tsdk: "))


class FakeCamera:
    """Opened camera whose first reads return no frame, as AVFoundation does on macOS."""

    def __init__(self, empty_reads):
        self.empty_reads = empty_reads
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self.empty_reads:
            self.empty_reads -= 1
            return False, None
        return True, numpy.zeros((720, 1280, 3), dtype=numpy.uint8)

    def release(self):
        self.released = True


class DefaultFrameSizeTest(unittest.TestCase):
    def test_waits_for_the_first_frame(self):
        camera = FakeCamera(empty_reads=5)
        with patch("tsdk.providers._open_camera", return_value=camera):
            self.assertEqual(default_frame_size(0), (1280, 720))
        self.assertTrue(camera.released)

    def test_camera_without_frames_fails_after_timeout(self):
        camera = FakeCamera(empty_reads=10**6)
        with patch("tsdk.providers._open_camera", return_value=camera):
            with self.assertRaisesRegex(RuntimeError, "Unable to read camera 2"):
                default_frame_size(2, timeout=0.2)
        self.assertTrue(camera.released)


class DeveloperExtraTest(unittest.TestCase):
    def test_dev_without_pyside6_names_the_extra(self):
        # Hide PySide6 as if the dev extra were not installed
        hidden = {name: None for name in sys.modules if name.partition(".")[0] == "PySide6"}
        hidden["PySide6"] = None
        stderr = io.StringIO()
        with patch.dict(sys.modules, hidden), contextlib.redirect_stderr(stderr):
            sys.modules.pop("tsdk.developer", None)
            code = main(["dev"])
        self.assertEqual(code, 1)
        self.assertIn("tsdk[dev]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

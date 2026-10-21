"""`tsdk play` reads TTEA's own calibration files, written here exactly as upstream TTEA writes them."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tsdk.cli import main
from tsdk.console import console_config

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


if __name__ == "__main__":
    unittest.main()

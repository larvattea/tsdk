import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tsdk.simulator import MAX_STRIDE, MIN_STANCE, DeveloperSimulator, FileSimulatorProvider, write_state
from tsdk.tracking import TrackingPipeline


class DeveloperSimulatorTest(unittest.TestCase):
    def test_placed_foot_reaches_game_feet_within_stride_limits(self):
        provider = DeveloperSimulator("body")
        right = provider.foot("right")
        provider.place_foot("left", .45, .7)
        feet = TrackingPipeline("body").process(provider.read()).feet
        self.assertAlmostEqual(feet["left"].x, .45)
        self.assertAlmostEqual(feet["left"].y, .7)
        self.assertAlmostEqual(feet["right"].x, right[0])
        self.assertAlmostEqual(feet["right"].y, right[1])
        provider.place_foot("left", -2., right[1])
        self.assertAlmostEqual(math.dist(provider.foot("left"), right), MAX_STRIDE)
        self.assertLess(provider.foot("left")[0], right[0])
        provider.place_foot("left", *right)
        self.assertAlmostEqual(math.dist(provider.foot("left"), right), MIN_STANCE)
        self.assertLess(provider.foot("left")[0], right[0])
        body = provider.observation.landmarks["body"]
        middle = [(a+b)/2 for a, b in zip(provider.foot("left"), right)]
        hips = [(body[23].x+body[24].x)/2, (body[23].y+body[24].y)/2]
        self.assertAlmostEqual(hips[0], middle[0])
        for hip, knee, ankle in ((23, 25, 27), (24, 26, 28)):
            self.assertAlmostEqual(body[knee].x, (body[hip].x+body[ankle].x)/2)

    def test_both_feet_keep_their_midpoint_within_stride_limits(self):
        provider = DeveloperSimulator("body")
        provider.set_feet((.1, .5), (.9, .5))
        self.assertAlmostEqual(math.dist(provider.foot("left"), provider.foot("right")), MAX_STRIDE)
        self.assertAlmostEqual((provider.foot("left")[0]+provider.foot("right")[0])/2, .5)
        provider.set_feet((.5, .5), (.5, .5))
        self.assertAlmostEqual(math.dist(provider.foot("left"), provider.foot("right")), MIN_STANCE)
        self.assertLess(provider.foot("left")[0], provider.foot("right")[0])


class StateFileTest(unittest.TestCase):
    """Windows refuses to replace a file another process has open, and to open one being replaced."""

    def test_locked_state_file_skips_a_frame_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "simulation.json"
            panel = DeveloperSimulator("body")
            game = FileSimulatorProvider("body", path)
            with mock.patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertFalse(game.read().present)
            write_state(path, panel)
            first = game.read()
            panel.place_foot("left", .45, .7)
            with mock.patch.object(Path, "replace", side_effect=PermissionError):
                write_state(path, panel)
            with mock.patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertIs(game.read(), first)
            write_state(path, panel)
            self.assertEqual(game.read().landmarks, panel.read().landmarks)


if __name__ == "__main__":
    unittest.main()

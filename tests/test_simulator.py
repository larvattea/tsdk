import math
import unittest

from ttea.sdk.tracking import Landmark, TrackingPipeline

from tsdk.simulator import MAX_STRIDE, MIN_STANCE, DeveloperSimulator, pointing_down_fingertip


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

    def test_index_pointing_down_needs_other_fingers_folded(self):
        def flipped(points):  # mirror about the wrist height: fingers point down
            return [Landmark(p.x, 2 * points[0].y - p.y, p.z) for p in points]
        provider = DeveloperSimulator("hands")
        open_hand = provider.observation.landmarks["left"]
        provider.preset("left", "fist")
        fist = provider.observation.landmarks["left"]
        upright = open_hand[:9] + fist[9:]
        pointing = flipped(upright)
        self.assertEqual(pointing_down_fingertip(pointing), pointing[8])
        self.assertIsNone(pointing_down_fingertip(upright))
        self.assertIsNone(pointing_down_fingertip(flipped(open_hand)))
        self.assertIsNone(pointing_down_fingertip(flipped(fist)))
        self.assertIsNone(pointing_down_fingertip(None))

    def test_both_feet_keep_their_midpoint_within_stride_limits(self):
        provider = DeveloperSimulator("body")
        provider.set_feet((.1, .5), (.9, .5))
        self.assertAlmostEqual(math.dist(provider.foot("left"), provider.foot("right")), MAX_STRIDE)
        self.assertAlmostEqual((provider.foot("left")[0]+provider.foot("right")[0])/2, .5)
        provider.set_feet((.5, .5), (.5, .5))
        self.assertAlmostEqual(math.dist(provider.foot("left"), provider.foot("right")), MIN_STANCE)
        self.assertLess(provider.foot("left")[0], provider.foot("right")[0])


if __name__ == "__main__":
    unittest.main()

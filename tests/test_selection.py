import unittest

import pygame

from tsdk.api import DWELL_SECONDS, LEAVE_TOLERANCE, SELECT_MARGIN, FloorSelector, feet_centre
from tsdk.tracking import Landmark, Observation, TrackingFrame

STEP = 0.25  # exact in binary floating point
BUTTONS = {"a": pygame.Rect(0, 0, 100, 100), "b": pygame.Rect(110, 0, 100, 100)}
ON_A = (50, 50)
OFF = (500, 500)


def run(selector, cursor, seconds, buttons=BUTTONS):
    results = [selector.update(cursor, buttons, STEP) for _ in range(round(seconds / STEP))]
    return [r for r in results if r is not None]


class FloorSelectorTest(unittest.TestCase):
    def setUp(self):
        self.selector = FloorSelector()
        self.assertIsNone(self.selector.update(ON_A, BUTTONS, STEP))  # entering starts the count at 0
        self.assertEqual(self.selector.selecting, "a")

    def test_dwell_completes_at_dwell_seconds_and_not_before(self):
        self.assertEqual(run(self.selector, ON_A, DWELL_SECONDS - STEP), [])
        self.assertEqual(self.selector.progress, {"a": (DWELL_SECONDS - STEP) / DWELL_SECONDS})
        self.assertEqual(self.selector.update(ON_A, BUTTONS, STEP), "a")
        self.assertIsNone(self.selector.selecting)
        self.assertEqual(self.selector.progress, {})

    def test_short_leave_keeps_count(self):
        run(self.selector, ON_A, 1.0)
        self.assertEqual(run(self.selector, OFF, LEAVE_TOLERANCE), [])
        self.assertEqual(self.selector.selecting, "a")
        self.assertEqual(self.selector.progress, {"a": (1.0 + LEAVE_TOLERANCE) / DWELL_SECONDS})
        self.assertEqual(run(self.selector, ON_A, DWELL_SECONDS - 1.0 - LEAVE_TOLERANCE), ["a"])

    def test_long_leave_resets(self):
        run(self.selector, ON_A, 1.0)
        run(self.selector, None, LEAVE_TOLERANCE + STEP)
        self.assertIsNone(self.selector.selecting)
        self.assertEqual(self.selector.progress, {})

    def test_margin_keeps_selection_without_entering_neighbour(self):
        edge = (100 + SELECT_MARGIN - 1, 50)  # outside a's plain rect, inside its grown rect
        self.assertFalse(BUTTONS["a"].collidepoint(edge))
        run(self.selector, edge, 1.0)
        self.assertEqual(self.selector.selecting, "a")
        self.assertEqual(self.selector.away, 0.0)
        inside_b = (120, 50)  # in b's plain rect but also in a's grown rect
        self.assertEqual(run(self.selector, inside_b, 1.0 - STEP), [])
        self.assertEqual(self.selector.selecting, "a")
        far_in_b = (200, 50)  # past a's margin: switches to b and restarts
        self.selector.update(far_in_b, BUTTONS, STEP)
        self.assertEqual(self.selector.progress, {"b": 0.0})


def frame(left=(0.2, 0.4), right=(0.4, 0.6), missing=False, stale=False, out_of_area=None):
    feet = {side: Landmark(*point) for side, point in (("left", left), ("right", right)) if point is not None}
    return TrackingFrame(Observation("body", 0.0), {}, feet, missing, stale, out_of_area or {})


class FeetCentreTest(unittest.TestCase):
    def test_midpoint(self):
        x, y = feet_centre(frame())
        self.assertAlmostEqual(x, 0.3)
        self.assertAlmostEqual(y, 0.5)

    def test_none_when_unusable(self):
        self.assertIsNone(feet_centre(None))
        self.assertIsNone(feet_centre(frame(missing=True)))
        self.assertIsNone(feet_centre(frame(stale=True)))
        self.assertIsNone(feet_centre(frame(left=None)))
        self.assertIsNone(feet_centre(frame(right=None)))
        self.assertIsNone(feet_centre(frame(out_of_area={"left": True, "right": False})))
        self.assertIsNone(feet_centre(frame(out_of_area={"right": True})))


if __name__ == "__main__":
    unittest.main()

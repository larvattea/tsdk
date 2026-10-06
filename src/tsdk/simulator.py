"""Developer-mode synthetic controls: skeleton labels, foot placement, and hand gestures."""
from __future__ import annotations

import math

from ttea.sdk.simulator import SimulatorProvider
from ttea.sdk.tracking import Landmark

BODY_NAMES = (
    'nose left_eye_inner left_eye left_eye_outer right_eye_inner right_eye '
    'right_eye_outer left_ear right_ear mouth_left mouth_right left_shoulder '
    'right_shoulder left_elbow right_elbow left_wrist right_wrist left_pinky '
    'right_pinky left_index right_index left_thumb right_thumb left_hip right_hip '
    'left_knee right_knee left_ankle right_ankle left_heel right_heel left_toe right_toe'
).split()
HAND_NAMES = ['wrist'] + [f'{finger}_{joint}' for finger in ('thumb', 'index', 'middle', 'ring', 'pinky') for joint in ('base', 'proximal', 'distal', 'tip')]
BODY_EDGES = [(11,12),(11,13),(13,15),(12,14),(14,16),(11,23),(12,24),(23,24),(23,25),(25,27),(24,26),(26,28),(27,29),(29,31),(28,30),(30,32)]
HAND_EDGES = [(0, 1 + finger * 4) for finger in range(5)] + [(i, i+1) for start in (1,5,9,13,17) for i in range(start,start+3)]
# Hip, knee, ankle, heel, toe landmark indices per side.
LEG = {'left': (23, 25, 27, 29, 31), 'right': (24, 26, 28, 30, 32)}
# Foot-to-foot limits in image units for the standing figure (about .8 tall).
MAX_STRIDE = .55
MIN_STANCE = .08
COINCIDENT = 1e-9  # closer feet have no meaningful direction; place them side by side
# Wrist and finger knuckles: their mean is the palm centre.
PALM = (0, 5, 9, 13, 17)


class DeveloperSimulator(SimulatorProvider):
    """TTEA's synthetic provider plus the foot placement developer mode drives with mouse, keys, or camera."""

    def foot(self, side):
        """Body foot point as the tracking pipeline derives it: the heel/toe midpoint."""
        points = self.observation.landmarks['body']
        heel, toe = points[LEG[side][3]], points[LEG[side][4]]
        return (heel.x+toe.x)/2, (heel.y+toe.y)/2

    def place_foot(self, side, x, y):
        """Plant one foot near (x, y), between MIN_STANCE and MAX_STRIDE from the other, which stays put."""
        other = self.foot('right' if side == 'left' else 'left')
        dx, dy = x-other[0], y-other[1]
        distance = math.hypot(dx, dy)
        limit = min(max(distance, MIN_STANCE), MAX_STRIDE)
        if distance < COINCIDENT:
            dx, dy, distance = (-1. if side == 'left' else 1.), 0., 1.
        target = other[0]+dx*limit/distance, other[1]+dy*limit/distance
        self.set_feet(*((target, other) if side == 'left' else (other, target)))

    def set_feet(self, left, right):
        """Plant both feet around their midpoint, between MIN_STANCE and MAX_STRIDE apart.

        The upper body follows the midpoint between the feet; knees stay between hip and ankle.
        """
        middle = (left[0]+right[0])/2, (left[1]+right[1])/2
        dx, dy = right[0]-left[0], right[1]-left[1]
        distance = math.hypot(dx, dy)
        half = min(max(distance, MIN_STANCE), MAX_STRIDE)/2
        if distance < COINCIDENT:
            dx, dy, distance = 1., 0., 1.
        targets = {'left': (middle[0]-dx*half/distance, middle[1]-dy*half/distance),
                   'right': (middle[0]+dx*half/distance, middle[1]+dy*half/distance)}
        points = self.observation.landmarks['body']
        before = [(a+b)/2 for a, b in zip(self.foot('left'), self.foot('right'))]
        for side, (x, y) in targets.items():
            current = self.foot(side)
            for index in LEG[side][2:]:
                point = points[index]
                points[index] = Landmark(point.x+x-current[0], point.y+y-current[1], point.z, point.visibility)
        for index in range(25):
            point = points[index]
            points[index] = Landmark(point.x+middle[0]-before[0], point.y+middle[1]-before[1], point.z, point.visibility)
        for hip, knee, ankle, _, _ in LEG.values():
            points[knee] = Landmark((points[hip].x+points[ankle].x)/2, (points[hip].y+points[ankle].y)/2,
                                    points[knee].z, points[knee].visibility)
        self._sync_world()


def pointing_down_fingertip(hand):
    """Index fingertip when a 21-point hand points it down with the other fingers folded, else None.

    Extended: the fingertip is farther from the wrist than its PIP joint; folded: closer.
    Down: the fingertip is below the index knuckle in the image.
    """
    if not hand or len(hand) < 21:
        return None
    wrist = hand[0]
    reach = [math.hypot(point.x-wrist.x, point.y-wrist.y) for point in hand]
    pointing = reach[8] > reach[6] and hand[8].y > hand[5].y
    folded = reach[12] < reach[10] and reach[16] < reach[14] and reach[20] < reach[18]
    return hand[8] if pointing and folded else None


def palm_centre(hand):
    """Centre of a 21-point hand's palm, whatever the fingers do; None without a hand."""
    if not hand or len(hand) < 21:
        return None
    return Landmark(sum(hand[i].x for i in PALM)/len(PALM), sum(hand[i].y for i in PALM)/len(PALM))

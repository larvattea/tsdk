"""Synthetic tracking, atomic local developer-process transport, and developer-mode body simulation."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import time

from .tracking import Landmark, Observation, observation_from_dict, observation_to_dict
from ._face_fixture import FACE_VERTICES

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


def _body():
    xy = [(0.5,.15),(.48,.13),(.46,.13),(.44,.13),(.52,.13),(.54,.13),(.56,.13),(.41,.16),(.59,.16),(.47,.19),(.53,.19),(.38,.3),(.62,.3),(.29,.45),(.71,.45),(.22,.6),(.78,.6),(.2,.62),(.8,.62),(.21,.64),(.79,.64),(.24,.61),(.76,.61),(.43,.55),(.57,.55),(.42,.73),(.58,.73),(.41,.89),(.59,.89),(.4,.92),(.6,.92),(.36,.94),(.64,.94)]
    return [Landmark(x,y,0.,1.) for x,y in xy]


def _hand(side):
    center = .32 if side == 'left' else .68
    sign = 1 if side == 'left' else -1
    points = [Landmark(center,.72,0.)]
    for finger in range(5):
        for joint in range(4):
            points.append(Landmark(center + sign * ((finger-2)*.045), .62 - joint * (.055 if finger != 0 else .035), 0.))
    return points


def _face():
    points = [Landmark(.5+x/40, .48-y/40, -z/40) for x,y,z in FACE_VERTICES]
    # Authored iris centers/rings extend the canonical 468 vertices in Tasks order.
    for outer, inner, upper, lower in ((33,133,159,145),(362,263,386,374)):
        cx = (points[outer].x+points[inner].x)/2
        cy = (points[upper].y+points[lower].y)/2
        cz = (points[outer].z+points[inner].z)/2
        radius = abs(points[outer].x-points[inner].x)*.18
        points.extend(Landmark(cx+dx,cy+dy,cz) for dx,dy in
                      ((0,0),(radius,0),(0,-radius),(-radius,0),(0,radius)))
    return points


class SimulatorProvider:
    """Editable authored observations; stale mode freezes time, not landmarks.

    Synthetic world coordinates use fixed meter scales (body 2, hands .5 per
    image unit), centered on the hip midpoint or hand centroid. Translation
    preserves them. They are not estimated measurements.
    Face world coordinates are unsupported. Canonical face coordinates and
    authored iris geometry are development fixtures, not physiology.
    """
    def __init__(self, profile):
        if profile not in ('body','hands','face'):
            raise ValueError(f'Unknown tracking profile: {profile}')
        self.profile = profile
        self.stale = False
        groups = {'body': _body()} if profile == 'body' else ({side:_hand(side) for side in ('left','right')} if profile == 'hands' else {'face':_face()})
        self.observation = Observation(
            profile=profile, timestamp=time.monotonic(), landmarks=groups,
            handedness={side: {'label':side, 'score':None} for side in groups} if profile == 'hands' else {},
            confidence_source='authored')
        self._sync_world()

    def _sync_world(self):
        world = {}
        for key, points in self.observation.landmarks.items():
            if key == 'face':
                continue
            origin = [points[23],points[24]] if key == 'body' else points
            center = [sum(getattr(p,axis) or 0. for p in origin)/len(origin) for axis in ('x','y','z')]
            # Fixed authored scales in meters, independent of image translation.
            scale = 2. if key == 'body' else .5
            world[key] = [Landmark((p.x-center[0])*scale,(p.y-center[1])*scale,
                                   ((p.z or 0.)-center[2])*scale,p.visibility) for p in points]
        self.observation = replace(self.observation, world_landmarks=world)

    def read(self):
        if not self.stale:
            self.observation = replace(self.observation, timestamp=time.monotonic())
        return observation_from_dict(observation_to_dict(self.observation))

    def move(self, group, dx=0., dy=0., dz=0.):
        self.observation.landmarks[group] = [Landmark(p.x+dx,p.y+dy,(p.z or 0.)+dz,p.visibility) for p in self.observation.landmarks[group]]
        self._sync_world()

    def close(self):
        pass


class DeveloperSimulator(SimulatorProvider):
    """The synthetic provider plus the foot placement developer mode drives with mouse, keys, or camera."""

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


def palm_centre(hand):
    """Centre of a 21-point hand's palm, whatever the fingers do; None without a hand."""
    if not hand or len(hand) < 21:
        return None
    return Landmark(sum(hand[i].x for i in PALM)/len(PALM), sum(hand[i].y for i in PALM)/len(PALM))


def write_state(path, provider):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    observation = provider.read()
    temporary.write_text(json.dumps({'observation':observation_to_dict(observation), 'stale':provider.stale}, allow_nan=False), encoding='utf-8')
    try:
        temporary.replace(path)
    except PermissionError:
        pass  # Windows: the game has the file open; the next tick writes it again
    return observation


class FileSimulatorProvider:
    """Consume atomic panel snapshots without a camera or network listener."""
    def __init__(self, profile, path):
        self.profile = profile
        self.path = Path(path)
        self.observation = None

    def read(self):
        try:
            text = self.path.read_text(encoding='utf-8')
        except PermissionError:
            # Windows: the panel is replacing the file; keep the last snapshot
            return self.observation or Observation(self.profile, 0.0, {}, present=False)
        state = json.loads(text)
        observation = observation_from_dict(state['observation'])
        if observation.profile != self.profile:
            raise ValueError('Simulator profile does not match game profile')
        self.observation = observation
        return observation

    def close(self):
        pass

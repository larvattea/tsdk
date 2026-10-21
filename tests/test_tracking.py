import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace

from tsdk.providers import MODELS, HardwareProvider
from tsdk.simulator import FileSimulatorProvider, SimulatorProvider, write_state
from tsdk.tracking import Landmark, Observation, Recording, ReplayProvider, TrackingPipeline


class TrackingSemanticsTest(unittest.TestCase):
    def body(self, timestamp=10.0):
        points = [Landmark(0.5, 0.5, visibility=0.9) for _ in range(33)]
        points[29] = Landmark(-0.2, 0.8, visibility=0.8)
        points[31] = Landmark(-0.1, 0.9, visibility=0.7)
        return Observation("body", timestamp, {"body": points})

    def test_outside_feet_are_not_clamped(self):
        frame = TrackingPipeline("body").process(self.body(), now=10.0)
        self.assertLess(frame.feet["left"].x, 0)
        self.assertTrue(frame.out_of_area["left"])
        self.assertFalse(frame.missing)
        self.assertFalse(frame.stale)

    def test_tracking_loss_and_recovery_do_not_create_origin_points(self):
        pipeline = TrackingPipeline("body")
        pipeline.process(self.body(), now=10.0)
        lost = pipeline.process(Observation("body", 10.1, {}, present=False), now=10.1)
        self.assertTrue(lost.missing)
        self.assertEqual(lost.feet, {})
        recovered = pipeline.process(self.body(10.2), now=10.2)
        self.assertFalse(recovered.missing)
        self.assertAlmostEqual(recovered.feet["left"].x, -0.15)

    def test_stale_observation_retains_raw_data_not_playable_feet(self):
        observation = self.body()
        frame = TrackingPipeline("body").process(observation, now=12.0)
        self.assertTrue(frame.stale)
        self.assertEqual(frame.feet, {})
        self.assertEqual(frame.observation.landmarks["body"][29].visibility, 0.8)

    def test_recorded_observations_replay_through_same_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            recording = Recording(path, "body", config={"stale_after": 0.5})
            observation = self.body()
            recording.write(observation, now=10.0)
            recording.close()
            replay = ReplayProvider(path)
            try:
                replayed = replay.read()
                expected = TrackingPipeline("body").process(observation, now=10.0)
                actual = TrackingPipeline("body").process(replayed, now=replay.now)
                self.assertEqual(actual.feet, expected.feet)
                self.assertEqual(actual.out_of_area, expected.out_of_area)
                with self.assertRaises(StopIteration):
                    replay.read()
            finally:
                replay.close()


class ProviderContractTest(unittest.TestCase):
    def test_hand_confidence_and_visibility_survive_recording(self):
        simulator = SimulatorProvider("hands")
        observation = simulator.read()
        observation.landmarks["left"][8] = Landmark(.2, .3, -.1, .7)
        self.assertEqual(observation.handedness, {"left": {"label": "left", "score": None},
                                                  "right": {"label": "right", "score": None}})
        self.assertIsNone(observation.landmarks["left"][0].visibility)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hands.jsonl"
            with Recording(path, "hands") as recording:
                recording.write(observation, now=observation.timestamp)
            with ReplayProvider(path) as replay:
                restored = replay.read()
        self.assertEqual(restored, observation)
        self.assertEqual(restored.confidence_source, "authored")
        self.assertEqual(restored.landmarks["left"][8].visibility, .7)

    def test_hardware_duplicate_hand_does_not_retain_other_detection_world(self):
        provider = HardwareProvider.__new__(HardwareProvider)
        provider.profile = "hands"
        point = SimpleNamespace(x=.2, y=.3, z=.1)
        result = SimpleNamespace(
            hand_landmarks=[[point], [SimpleNamespace(x=.8,y=.3,z=.1)]],
            hand_world_landmarks=[[point]],
            handedness=[[SimpleNamespace(category_name="Left",score=.6)],
                        [SimpleNamespace(category_name="Left",score=.9)]])
        observation = provider._observation(result, 1.)
        self.assertEqual(observation.handedness["left"], {"label":"left","score":.9})
        self.assertEqual(observation.landmarks["left"][0].x, .8)
        self.assertEqual(observation.world_landmarks, {})
        self.assertEqual(observation.confidence_source, "measured")

    def test_hardware_models_ship_with_tsdk(self):
        for name in ("pose_landmarker_full.task", "hand_landmarker.task", "face_landmarker.task"):
            self.assertGreater((MODELS / name).stat().st_size, 0)

    def test_model_coordinates_ignore_translation(self):
        for profile,group in (("body","body"),("hands","left")):
            with self.subTest(profile=profile):
                provider = SimulatorProvider(profile)
                before = provider.read()
                provider.move(group,.2,-.1,.3)
                moved = provider.read()
                self.assertAlmostEqual(moved.landmarks[group][0].x, before.landmarks[group][0].x+.2)
                for a,b in zip(before.world_landmarks[group],moved.world_landmarks[group]):
                    for axis in ("x","y","z"):
                        self.assertAlmostEqual(getattr(a,axis),getattr(b,axis))

    def test_face_index_geometry(self):
        provider = SimulatorProvider("face")
        neutral = provider.read()
        points = neutral.landmarks["face"]
        self.assertLess(points[33].x, points[133].x)
        self.assertLess(points[362].x, points[263].x)
        self.assertLess(points[159].y, points[145].y)
        self.assertLess(points[13].y, points[14].y)
        self.assertLess(points[10].y, points[152].y)
        self.assertLess(points[33].x, points[468].x)
        self.assertLess(points[468].x, points[133].x)
        self.assertEqual(neutral.world_landmarks, {})
        self.assertTrue(all(p.visibility is None for p in points))
        original = points[477]
        provider.observation.landmarks["face"][477] = Landmark(1.2,-.2,.4)
        self.assertEqual(neutral.landmarks["face"][477], original)

    def test_file_delivery_recording_stale_and_recovery_for_each_profile(self):
        for profile,group in (("body","body"),("hands","left"),("face","face")):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                provider = SimulatorProvider(profile)
                path = Path(directory)/"state.json"
                consumer = FileSimulatorProvider(profile,path)
                provider.observation.landmarks[group][0] = Landmark(1.2,.3,-.1)
                provider.stale = True
                observation = write_state(path,provider)
                delivered = consumer.read()
                pipeline = TrackingPipeline(profile)
                frame = pipeline.process(delivered,now=delivered.timestamp)
                self.assertTrue(frame.out_of_area[group][0])
                self.assertTrue(pipeline.process(delivered,now=delivered.timestamp+1).stale)
                recording_path = Path(directory)/"recording.jsonl"
                with Recording(recording_path,profile,config={"stale_after":.5}) as recording:
                    recording.write(observation,now=observation.timestamp+1)
                with ReplayProvider(recording_path) as replay:
                    replayed = replay.read()
                    self.assertTrue(TrackingPipeline(profile,config=replay.config).process(
                        replayed,now=replay.now).stale)
                provider.stale = False
                write_state(path,provider)
                recovered = consumer.read()
                self.assertFalse(pipeline.process(recovered,now=recovered.timestamp).stale)
                self.assertEqual(recovered.landmarks[group][0].x,1.2)


if __name__ == "__main__":
    unittest.main()

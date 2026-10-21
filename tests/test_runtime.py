import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from tsdk.embedding import FrameReader
from tsdk.runtime import run_game
from tsdk.simulator import SimulatorProvider
from tsdk.tracking import Observation, Recording


CONSUMER = '''from tsdk.api import Game
import pygame

class Consumer(Game):
    def start(self, context):
        self.events = ["start"]
        self.updates = []
        self.draws = []
    def update(self, context):
        self.events.append("update")
        self.updates.append(context.tracking.observation.timestamp)
    def draw(self, context):
        self.events.append("draw")
        self.draws.append(context.pause_reason)
        context.surface.fill((12, 34, 56))
        if context.config.get("keyboard"):
            key = pygame.K_p if len(self.draws) < 3 else pygame.K_ESCAPE
            pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=key))
    def pause(self, context):
        self.events.append("pause")
    def resume(self, context):
        self.events.append("resume")
    def stop(self, context):
        self.events.append("stop")
        return {"events": self.events, "updates": self.updates, "draws": self.draws}
'''


class RuntimeLifecycleTest(unittest.TestCase):
    def test_recorded_loss_staleness_and_recovery_across_profiles(self):
        for profile in ("body", "hands", "face"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "consumer.py").write_text(CONSUMER, encoding="utf-8")
                manifest = {"id": "test.lifecycle", "tracking_profile": profile,
                            "entrypoint": "consumer:Consumer"}
                recording = root / "tracking.jsonl"
                simulator = SimulatorProvider(profile)
                with Recording(recording, profile) as writer:
                    for timestamp in (10.0, 11.0, 12.0, 13.0, 14.0):
                        observation = simulator.read()
                        observation.timestamp = timestamp
                        if timestamp in (11.0, 12.0):
                            observation = Observation(profile, timestamp, {}, present=False)
                        elif timestamp == 13.0:
                            observation.timestamp = 10.0
                        writer.write(observation, now=timestamp)
                simulator.close()
                expected = {
                    "events": ["start", "update", "draw", "pause", "draw", "draw",
                               "draw", "resume", "update", "draw", "stop"],
                    "updates": [10.0, 14.0],
                    "draws": [None, "tracking_loss", "tracking_loss", "tracking_loss", None],
                }
                config = {"session_root": str(root / "sessions"), "fps": 0}
                for _ in range(2):
                    self.assertEqual(run_game(root, manifest, replay=recording, config=config), expected)
                results = list((root / "sessions" / "test.lifecycle" / "anonymous").glob("result-*.json"))
                self.assertEqual(len(results), 2)
                for path in results:
                    self.assertEqual(json.loads(path.read_text())["result"], expected)

    def test_keyboard_pause_resume_and_escape_use_runtime_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "consumer.py").write_text(CONSUMER, encoding="utf-8")
            manifest = {"id": "test.keyboard", "tracking_profile": "body",
                        "entrypoint": "consumer:Consumer"}
            result = run_game(root, manifest, simulation=True, max_frames=10,
                              config={"keyboard": True, "fps": 0,
                                      "session_root": str(root / "sessions")})
            self.assertEqual(result["events"], ["start", "update", "draw", "pause", "draw",
                                                "resume", "update", "draw", "stop"])
            self.assertEqual(result["draws"], [None, "user", None])

    def test_embedded_runtime_takes_stdin_input_and_publishes_frames(self):
        game = '''from tsdk.api import Game
import pygame

class Embedded(Game):
    def start(self, context):
        self.seen = []
    def update(self, context):
        for event in context.events:
            if event.type == pygame.MOUSEBUTTONDOWN:
                self.seen.append(["mouse", list(event.pos), event.button])
            elif event.type == pygame.KEYDOWN:
                self.seen.append(["key", event.key, event.mod & pygame.KMOD_SHIFT != 0, event.unicode])
                context.request_stop()
    def draw(self, context):
        context.surface.fill((12, 34, 56))
    def stop(self, context):
        return {"seen": self.seen}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "embedded.py").write_text(game, encoding="utf-8")
            manifest = {"id": "test.embedded", "tracking_profile": "body", "entrypoint": "embedded:Embedded"}
            read_end, write_end = os.pipe()
            with os.fdopen(read_end) as stdin, os.fdopen(write_end, "w") as messages, \
                    mock.patch("sys.stdin", stdin):
                messages.write(json.dumps({"type": "mouse_down", "pos": [3, 4], "button": 1}) + "\n")
                messages.write(json.dumps({"type": "key_down", "key": "a", "text": "A", "mods": ["shift"]}) + "\n")
                messages.flush()
                result = run_game(root, manifest, simulation=True, max_frames=100000,
                                  config={"fps": 0, "width": 32, "height": 24, "input": "stdin",
                                          "frame_buffer": str(root / "frame.bin"),
                                          "session_root": str(root / "sessions"), "show_feet": False})
            self.assertEqual(result["seen"], [["mouse", [3, 4], 1], ["key", ord("a"), True, "A"]])
            frames = FrameReader(root / "frame.bin")
            width, height, pixels = frames.read()
            frames.close()
            self.assertEqual((width, height), (32, 24))
            self.assertEqual(pixels[:4], bytes((12, 34, 56, 255)))
            self.assertEqual(len(set(pixels[i:i + 4] for i in range(0, len(pixels), 4))), 1)


if __name__ == "__main__":
    unittest.main()

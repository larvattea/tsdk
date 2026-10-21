import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from tsdk.file_records import FileRecords
from tsdk.runtime import run_game


RECORDING_GAME = '''from tsdk.api import Game

class Recorder(Game):
    def start(self, context):
        self.config = context.records.configuration({"palette": 0, "sound": True})
        context.records.update_configuration({"level_id": 2})
        self.session = context.records.start_session({"score": 0})
        context.records.record_event(self.session, {"event_type": "start"})
    def stop(self, context):
        context.records.update_session(self.session, {"score": 5})
        return {"config": self.config, "session": self.session}
'''


class FileRecordsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "players" / "game.json"

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_configuration_merges_saved_values_over_defaults_and_persists(self):
        records = FileRecords(self.path)
        self.assertEqual(records.configuration({"palette": 0, "hud": True}), {"palette": 0, "hud": True})
        self.assertEqual(self.stored(), {"configuration": {"palette": 0, "hud": True}, "sessions": []})
        records.update_configuration({"palette": 4, "horizon_image": "horizon_night"})
        # A later game version adds a setting: saved values win, the new default is filled in and saved.
        expected = {"palette": 4, "hud": True, "horizon_image": "horizon_night", "sound": True}
        self.assertEqual(FileRecords(self.path).configuration({"palette": 0, "hud": True, "sound": True}), expected)
        self.assertEqual(self.stored()["configuration"], expected)

    def test_replace_configuration_keeps_sessions(self):
        records = FileRecords(self.path)
        records.configuration({"palette": 0, "hud": True})
        session = records.start_session({"score": 1})
        records.replace_configuration({"palette": 4})
        self.assertEqual(self.stored()["configuration"], {"palette": 4})
        self.assertEqual([s["id"] for s in self.stored()["sessions"]], [session])
        self.assertEqual(records.configuration({"palette": 0, "hud": True}), {"palette": 4, "hud": True})

    def test_sessions_number_from_one_and_collect_events(self):
        records = FileRecords(self.path)
        first = records.start_session({"score": 0})
        records.record_event(first, {"event_type": "a"})
        records.update_session(first, {"score": 7})
        second = FileRecords(self.path).start_session({"score": 0})
        records.record_event(second, {"event_type": "b"})
        records.record_event(second, {"event_type": "c"})
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.stored()["sessions"], [
            {"id": 1, "score": 7, "events": [{"event_type": "a"}]},
            {"id": 2, "score": 0, "events": [{"event_type": "b"}, {"event_type": "c"}]}])
        with self.assertRaises(KeyError):
            records.update_session(3, {"score": 1})
        with self.assertRaises(KeyError):
            records.record_event(3, {})
        with self.assertRaisesRegex(ValueError, "may not set"):
            records.update_session(1, {"id": 9})

    def test_failed_write_leaves_previous_file_and_no_temporary(self):
        records = FileRecords(self.path)
        records.configuration({"palette": 0})
        before = self.path.read_bytes()
        with mock.patch("tsdk.file_records.os.replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            records.update_configuration({"palette": 4})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        with self.assertRaises(ValueError):
            records.update_configuration({"palette": float("nan")})
        self.assertEqual(self.path.read_bytes(), before)

    def test_runtime_binds_records_file_from_config(self):
        root = self.path.parent.parent
        (root / "recorder.py").write_text(RECORDING_GAME, encoding="utf-8")
        manifest = {"id": "test.records", "tracking_profile": "body", "entrypoint": "recorder:Recorder"}
        config = {"records": str(self.path), "fps": 0, "session_root": str(root / "sessions")}
        first = run_game(root, manifest, simulation=True, max_frames=2, config=config)
        second = run_game(root, manifest, simulation=True, max_frames=2, config=config)
        self.assertEqual(first, {"config": {"palette": 0, "sound": True}, "session": 1})
        self.assertEqual(second, {"config": {"palette": 0, "sound": True, "level_id": 2}, "session": 2})
        self.assertEqual(self.stored()["sessions"][1], {"id": 2, "score": 5, "events": [{"event_type": "start"}]})


if __name__ == "__main__":
    unittest.main()

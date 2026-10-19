"""Registered-player mode of the developer panel, against the real runtime subprocess."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from tsdk.developer import DeveloperPanel, PlayerConfigDialog
from tsdk.template import new_project

GAME = '''from ttea.sdk.api import Game as BaseGame


class Game(BaseGame):
    def start(self, context):
        self.config = context.records.configuration({"palette": 0, "sound": True}) if context.records else None

    def stop(self, context):
        return {"config": self.config}
'''


class DeveloperPlayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = new_project(self.root / "game", "example.player", "Player", "body")
        (self.project / "game.py").write_text(GAME, encoding="utf-8")
        (self.root / "work").mkdir()
        self.panel = DeveloperPanel(self.root / "work", self.project)
        self.addCleanup(self.panel.shutdown)
        self.panel.players = self.root / "players"
        self.path = self.root / "players" / "example.player.json"

    def wait_for(self, condition):
        deadline = time.monotonic() + 20
        while not condition():
            self.assertIsNotNone(self.panel.process)
            self.assertIsNone(self.panel.process.poll(), "game exited early")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.05)

    def saved(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))["configuration"]
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def results(self):
        stored = (self.root / "work" / "sessions" / "example.player" / "anonymous").glob("result-*.json")
        return [json.loads(path.read_text())["result"]["config"] for path in stored]

    def test_registered_player_reads_and_resets_saved_configuration(self):
        self.panel.load_project(self.project)
        self.assertFalse(self.panel.player_config_button.isEnabled())
        self.wait_for(lambda: self.panel.frames.read() is not None)
        self.panel.stop_game()
        self.assertEqual(self.results(), [None])  # anonymous: no records
        self.assertFalse(self.path.exists())

        self.panel.registered.setChecked(True)
        self.assertTrue(self.panel.player_config_button.isEnabled())
        dialog = PlayerConfigDialog(self.panel, "example.player", self.path)
        self.assertEqual(json.loads(dialog.editor.toPlainText()), {})
        dialog.editor.setPlainText("[1]")
        dialog.save()
        self.assertIn("JSON object", dialog.error.text())
        dialog.editor.setPlainText('{"palette": 4, "horizon_image": "horizon_night"}')
        dialog.save()
        self.panel.apply_player_config(dialog.values)
        self.wait_for(lambda: "sound" in (self.saved() or {}))
        self.panel.stop_game()
        expected = {"palette": 4, "horizon_image": "horizon_night", "sound": True}
        self.assertEqual(self.saved(), expected)
        self.assertIn(expected, self.results())

        dialog = PlayerConfigDialog(self.panel, "example.player", self.path)
        self.assertEqual(json.loads(dialog.editor.toPlainText()), expected)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog.reset()
        self.assertIsNone(dialog.values)
        self.panel.apply_player_config(dialog.values)
        self.wait_for(lambda: self.saved() == {"palette": 0, "sound": True})


if __name__ == "__main__":
    unittest.main()

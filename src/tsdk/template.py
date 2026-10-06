import json
from pathlib import Path

from ttea.sdk.cartridge import validate_manifest

GAME = '''"""Draw tracked points using the platform-owned loop and observation pipeline."""
import pygame
from ttea.sdk.api import GameContext


class Game:
    def start(self, context: GameContext):
        self.active_seconds = 0.0
        self.pauses = 0

    def update(self, context: GameContext):
        self.active_seconds += context.dt

    def draw(self, context: GameContext):
        context.surface.fill((20, 25, 35))
        width, height = context.surface.get_size()
        for points in context.tracking.projection.values():
            for point in points:
                pygame.draw.circle(context.surface, (80, 220, 160),
                                   (round(point.x * width), round(point.y * height)), 5)

    def pause(self, context: GameContext):
        self.pauses += 1

    def resume(self, context: GameContext):
        pass

    def stop(self, context: GameContext):
        return {"active_seconds": self.active_seconds, "pauses": self.pauses}
'''


def new_project(destination, game_id, name, profile):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    manifest = {"id": game_id, "version": "0.1.0", "entrypoint": "game:Game", "tracking_profile": profile,
                "game_api": 1, "requires_ttea": ">=2.0,<3", "name": {"en": name}, "assets": [], "licenses": ["LICENSE"]}
    validate_manifest(manifest)
    destination.mkdir(parents=True)
    files = {
        "ttea.toml": f'''[game]
id = {json.dumps(game_id)}
version = "0.1.0"
entrypoint = "game:Game"
tracking_profile = "{profile}"
game_api = 1
requires_ttea = ">=2.0,<3"
assets = []
licenses = ["LICENSE"]

[game.name]
en = {json.dumps(name)}
''',
        "pyproject.toml": f'''[project]
name = {json.dumps(game_id)}
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["t-tea>=2.0,<3"]

[dependency-groups]
dev = ["tsdk>=0.1,<0.2"]

[tool.uv.sources]
t-tea = {{ path = "../ttea", editable = true }}
tsdk = {{ path = "../tsdk", editable = true }}
''',
        "game.py": GAME,
        ".gitignore": ".venv/\n__pycache__/\n*.pyc\ndist/\n",
        "LICENSE": "This generated example is dedicated to the public domain under CC0 1.0.\nhttps://creativecommons.org/publicdomain/zero/1.0/\nChoose appropriate licensing for your changes and added assets before distribution.\n",
    }
    for relative, content in files.items():
        (destination / relative).write_text(content, encoding="utf-8")
    return destination

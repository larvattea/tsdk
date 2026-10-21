"""TSDK-owned Pygame process for source projects and installed cartridges."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from .api import GameContext, SessionStorage
from .cartridge import localized, read_project, validate_manifest

DEFAULT_RESOLUTION = (800, 600)  # surface size when neither config nor ttea.toml sets one


def load_manifest(project: Path) -> dict:
    built = project / "manifest.json"
    if built.is_file():
        return validate_manifest(json.loads(built.read_text(encoding="utf-8")), built=True)
    return read_project(project)


def run_game(project: Path, manifest: dict, *, simulation: bool = False,
             replay: Path | None = None, max_frames: int | None = None,
             config: dict | None = None) -> dict:
    """Run one session, close owned resources, and persist its returned result.

    Escape/close terminates; P or F2 toggles user pause, shown by a shared overlay. Tracking loss also pauses
    updates, while drawing and event delivery continue until recovery.
    """
    import pygame

    from .feet import draw_feet
    from .overlay import draw_pause
    from .tracking import Observation, ReplayProvider, TrackingPipeline

    project = Path(project).resolve(strict=True)
    config = dict(config or {})
    if simulation and replay is not None:
        raise ValueError("Choose simulation or replay, not both")
    if max_frames is not None and max_frames < 0:
        raise ValueError("max_frames must not be negative")
    profile = manifest["tracking_profile"]
    with ExitStack() as resources:
        if config.get("frame_buffer"):
            # Developer mode shows the frames in its own window.
            os.environ["SDL_VIDEODRIVER"] = "dummy"
        if config.get("display") is not None:
            # The operator works on the other screen: losing focus must not minimize the game.
            os.environ["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"] = "0"
        pygame.init()
        resources.callback(pygame.quit)
        default_width, default_height = manifest.get("resolution", DEFAULT_RESOLUTION)
        size = (int(config.get("width", default_width)), int(config.get("height", default_height)))
        if config.get("display") is None:
            surface = pygame.display.set_mode(size)
        else:
            # Fill the projector. macOS keeps borderless windows under its menu bar; only fullscreen covers it.
            flags = pygame.FULLSCREEN if sys.platform == "darwin" else pygame.NOFRAME
            surface = pygame.display.set_mode(size, flags, display=int(config["display"]))
        frames_out = inputs = None
        if config.get("frame_buffer"):
            from .embedding import FrameWriter
            frames_out = FrameWriter(Path(config["frame_buffer"]), size)
            resources.callback(frames_out.close)
        if config.get("input") == "stdin":
            from .embedding import InputReader
            inputs = InputReader(sys.stdin)
        language = config.get("language", config.get("lang", "en"))
        pygame.display.set_caption(localized(manifest.get("name", {}), language, manifest["id"]))
        if replay is not None:
            provider = ReplayProvider(Path(replay))
        elif simulation:
            if config.get("simulation_state"):
                from .simulator import FileSimulatorProvider
                provider = FileSimulatorProvider(profile, Path(config["simulation_state"]))
            else:
                from .simulator import SimulatorProvider
                provider = SimulatorProvider(profile)
        else:
            from .providers import HardwareProvider
            provider = HardwareProvider(profile, **config.get("hardware", {}))
        resources.callback(provider.close)
        if replay is not None and provider.profile != profile:
            raise ValueError("Replay tracking profile differs from the game profile")
        calibration = provider.calibration if replay is not None else config.get("calibration")
        tracking_config = provider.config if replay is not None else config.get("tracking", {})
        pipeline = TrackingPipeline(profile, calibration=calibration, config=tracking_config)
        recorder = None
        if config.get("recording"):
            from .tracking import Recording
            recorder = Recording(Path(config["recording"]), profile, calibration=calibration, config=tracking_config)
            resources.callback(recorder.close)
        storage = SessionStorage(manifest["id"], str(config.get("player_id", "anonymous")),
                                 root=Path(config["session_root"]) if config.get("session_root") else None)
        context = GameContext(surface, pipeline.process(Observation(profile, 0.0, {}, present=False)),
                              0.0, 0.0, (), project, config, storage)
        if config.get("records"):
            # Registered-player records: one JSON file per player and game.
            from .file_records import FileRecords
            context.records = FileRecords(Path(config["records"]))
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.signal(signal.SIGTERM, lambda signum, frame: context.request_stop())
            resources.callback(signal.signal, signal.SIGTERM, previous_sigterm)
        old_path = sys.path[:]
        sys.path.insert(0, str(project))
        resources.callback(lambda: sys.path.__setitem__(slice(None), old_path))
        local_modules = {path.stem for path in project.iterdir()
                         if path.suffix == ".py" or (path.is_dir() and (path / "__init__.py").is_file())}
        displaced = {name: module for name, module in sys.modules.items()
                     if name.partition(".")[0] in local_modules}
        for name in displaced:
            del sys.modules[name]

        def restore_modules():
            for name in list(sys.modules):
                if name.partition(".")[0] in local_modules:
                    del sys.modules[name]
            sys.modules.update(displaced)

        resources.callback(restore_modules)
        importlib.invalidate_caches()
        module_name, class_name = manifest["entrypoint"].split(":", 1)
        game = getattr(importlib.import_module(module_name), class_name)()
        clock = pygame.time.Clock()
        user_paused = False
        frames = 0
        result = None
        stop_file = Path(config["stop_file"]) if config.get("stop_file") else None
        show_feet = bool(config.get("show_feet", True))
        try:
            game.start(context)
            while not context.stop_requested and (max_frames is None or frames < max_frames):
                context.dt = clock.tick(int(config.get("fps", 60))) / 1000.0
                context.elapsed += context.dt
                if inputs is not None:
                    if not inputs.post(pygame):
                        context.request_stop()
                    show_feet = bool(inputs.config.get("show_feet", show_feet))
                context.events = tuple(pygame.event.get())
                for event in context.events:
                    if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                        context.request_stop()
                    elif event.type == pygame.KEYDOWN and event.key in (pygame.K_p, pygame.K_F2):
                        user_paused = not user_paused
                if stop_file is not None and stop_file.exists():
                    context.request_stop()
                if context.stop_requested:
                    break
                try:
                    observation = provider.read()
                except StopIteration:
                    context.request_stop()
                    break
                now = provider.now if replay is not None else time.monotonic()
                if recorder is not None:
                    recorder.write(observation, now=now)
                context.tracking = pipeline.process(observation, now=now)
                lost = context.tracking.missing or context.tracking.stale
                reason = "user" if user_paused else "tracking_loss" if lost else None
                was_paused = context.paused
                context.paused = reason is not None
                context.pause_reason = reason
                if context.paused and not was_paused:
                    game.pause(context)
                elif was_paused and not context.paused:
                    game.resume(context)
                if not context.paused and not context.stop_requested:
                    game.update(context)
                game.draw(context)
                if show_feet and not context.tracking.missing:
                    outside = context.tracking.out_of_area
                    draw_feet(surface, {side: (point.x, point.y)
                                        for side, point in context.tracking.feet.items()
                                        if not outside.get(side)})
                if context.pause_reason == "user":
                    draw_pause(surface, str(language))
                pygame.display.flip()
                if frames_out is not None:
                    frames_out.write(pygame.image.tobytes(surface, "RGBX"))
                frames += 1
        finally:
            result = game.stop(context)
        if result is None:
            result = context.result if context.result is not None else {}
        if not isinstance(result, dict):
            raise TypeError("Game.stop must return a dictionary or None")
        storage.record_result(result)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--simulation", action="store_true")
    mode.add_argument("--replay", type=Path)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--config", default="{}", help="JSON session configuration")
    args = parser.parse_args(argv)
    config = json.loads(args.config)
    if not isinstance(config, dict):
        parser.error("--config must be a JSON object")
    result = run_game(args.project, load_manifest(args.project), simulation=args.simulation,
                      replay=args.replay, max_frames=args.max_frames, config=config)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Exercise the actual built archive without its source checkout on sys.path."""
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ttea.sdk.cartridge import extract_cartridge
from ttea.sdk.runtime import run_game
from ttea.sdk.tracking import Landmark, Observation, Recording


def main():
    archive = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="tsdk-ci-") as temporary:
        root = Path(temporary)
        project = root / "installed"
        manifest = extract_cartridge(archive, project)
        os.chdir(root)
        config = {"session_root": str(root / "sessions"), "autostart": True}
        simulated = run_game(project, manifest, simulation=True, max_frames=8, config=config)
        profile = manifest["tracking_profile"]
        key = {"body": "body", "hands": "left", "face": "face"}[profile]
        count = {"body": 33, "hands": 21, "face": 478}[profile]
        recording_path = root / "replay.jsonl"
        recording = Recording(recording_path, profile)
        for index in range(12):
            present = index not in (3, 4, 5)
            observation = Observation(profile=profile, timestamp=index / 30,
                                      landmarks={key: [Landmark(.5, .5, visibility=1.0) for _ in range(count)]} if present else {},
                                      present=present)
            recording.write(observation, now=index / 30)
        recording.close()
        callbacks = []
        def observe_callback(frame, event, arg):
            if event == "call" and frame.f_code.co_name in {"start", "update", "pause", "resume", "stop"}:
                context = frame.f_locals.get("context")
                if context is not None and hasattr(context, "pause_reason"):
                    callbacks.append((frame.f_code.co_name, context.pause_reason))
        previous_profile = sys.getprofile()
        sys.setprofile(observe_callback)
        try:
            replayed = run_game(project, manifest, replay=recording_path, max_frames=12, config=config)
        finally:
            sys.setprofile(previous_profile)
        pause = callbacks.index(("pause", "tracking_loss"))
        resume = callbacks.index(("resume", None), pause + 1)
        if not any(name == "update" for name, _ in callbacks[:pause]):
            raise AssertionError("Replay must update before tracking loss")
        if any(name == "update" for name, _ in callbacks[pause:resume]):
            raise AssertionError("Game updated while tracking was missing")
        if not any(name == "update" for name, _ in callbacks[resume:]):
            raise AssertionError("Replay must resume updates after tracking recovery")
        if callbacks[0][0] != "start" or callbacks[-1][0] != "stop":
            raise AssertionError("Runtime must start and stop the game lifecycle")
        if not isinstance(simulated, dict) or not isinstance(replayed, dict):
            raise AssertionError("Runtime must return session result dictionaries")
        print({"simulation": simulated, "loss_recovery_replay": replayed})


if __name__ == "__main__":
    main()

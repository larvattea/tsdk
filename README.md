# TSDK: SDK for Serious Exergames

Build games for [TTEA](https://github.com/laurogripa/ttea), the interactive-floor exergame console.

TSDK creates, checks, packages, and test-runs games. Every game runs on TTEA's own runtime, tracking, and camera code.

## Start a game in six commands

Keep `ttea`, `tsdk`, and your games in the same parent directory, then run:

```sh
cd tsdk
uv sync --locked
uv run tsdk new ../example-game --profile body
cd ../example-game
uv lock
uv run tsdk dev
```

| Command | Result |
| --- | --- |
| `tsdk new DEST [--id] [--name] [--profile body\|hands\|face]` | New project with `ttea.toml`, `pyproject.toml`, `game.py`, and `LICENSE` |
| `tsdk check [project]` | Validates the manifest and source without running the game |
| `tsdk dev [project]` | Opens developer mode |
| `tsdk build [project] [--output PATH] [--release]` | Writes `dist/<id>-<version>.ttea` |

Commit each game's `uv.lock`. The `../` paths in `[tool.uv.sources]` serve local development only; change them for another layout.

## A `.ttea` file is a game TTEA can install

A `.ttea` file is a ZIP archive. TTEA installs it, validates its manifest, and runs the game. It holds:

- the entrypoint module, root Python modules, and top-level packages
- the assets and license notices listed in `ttea.toml`
- `manifest.json`, generated from `ttea.toml` at build time

You author `ttea.toml`. This one is from Ginga Divina:

```toml
[game]
id = "gingadivina"
version = "2.0.0"
entrypoint = "game.main:GingaDivina"
tracking_profile = "body"   # body, hands, or face
resolution = [800, 600]     # optional; default 1280x720
game_api = 1
requires_ttea = ">=2.0,<3"
assets = ["assets"]
licenses = ["LICENSE", "CREDITS.md"]

[game.name]
pt = "Ginga Divina"
en = "Ginga Divina"
```

The build adds provenance to `manifest.json`; games cannot set it themselves:

| Field | Value |
| --- | --- |
| `format_version` | `1` |
| `builder.version` | Installed `tsdk` version |
| `build.source_commit` | Git `HEAD`, or `"unversioned"` outside Git |
| `build.dirty` | `true` with uncommitted or untracked files |

## Developer mode runs your game beside tracking tools

`uv run tsdk dev [project]` opens a full-screen window with no game loaded.
Load game starts browsing from `project` (default: the current directory) and accepts project folders or `.ttea` files.

- **Left two thirds**: the game, running in TTEA's runtime.
- **Right third**: tracking source, skeleton preview, record and replay, and Load/Restart/Stop game (Exit once the game stops).

| Tracking source | How it moves the player |
| --- | --- |
| Synthetic | Edit presets, joints, presence, and stale timestamps in the panel |
| Mouse + keyboard | The mouse over the skeleton moves the whole player; hold A or D to plant the left or right foot |
| Camera | Body pose, both index fingers pointing down, both whole hands, or the nose as the centre of the feet |

Synthetic and mouse sources need no camera, player registration, or calibration. The webcam opens only when you select Camera.
Sessions use temporary storage.

> **Note:** The runtime renders off-screen, so `pygame.mouse.get_pos` and `pygame.key.get_pressed` stay idle. Read input from `context.events`.

## Release builds need a clean Git checkout

Exclude `dist/` in `.gitignore`, commit your changes, then run:

```sh
uv run tsdk build --release --output dist/game.ttea
```

Release builds fail on unversioned source, modified files, or untracked files.
Every packaged file must be tracked, including assets your `.gitignore` excludes.
The output path cannot overwrite packaged source, `ttea.toml`, or `pyproject.toml`.

## Checks enforce the TTEA game contract

`tsdk check` reads `ttea.toml` through `ttea.sdk.cartridge` and parses source without importing it.

- **Dependencies**: `pyproject.toml` lists only `t-tea`, with the same bounded range as `requires_ttea`. Put tools in a development group.
- **Imports**: standard library, `pygame`, `numpy`, `ttea.sdk.api`, `ttea.sdk.tracking`, and the game's own modules.
- **Calls**: no display setup, event pumping, clock, camera, `subprocess`, `eval`/`exec`, or dependency installation.
- **Files**: no symlinks, hidden paths, compiled binaries, ML models, installers, or bundled platform libraries.
- **Lifecycle**: the entrypoint class defines `start`, `update`, `draw`, `pause`, `resume`, and `stop` as synchronous `(self, context)` methods, or inherits them from `ttea.sdk.api.Game`.

The checks enforce conventions; they are not a security sandbox.

## Game repositories reuse one CI workflow

```yaml
jobs:
  tsdk:
    uses: laurogripa/tsdk/.github/workflows/game.yml@main
    with:
      ttea-ref: main
      builder-ref: main
    secrets: inherit
```

| Input | Default | Purpose |
| --- | --- | --- |
| `ttea-ref`, `builder-ref` | required | TTEA and TSDK commits; pin reviewed ones |
| `project-path` | `.` | Game directory inside the repository |
| `python-versions` | `["3.10", "3.12"]` | Python versions to test |
| `release` | `false` | Build with `--release` |

On Windows x86-64, Linux x86-64, and macOS ARM64, the workflow checks the game, runs its tests, and builds the cartridge.
It then installs and runs the cartridge outside the checkout, including synthetic tracking loss and recovery.
Private TTEA and TSDK repositories need the `TTEA_DEPLOY_KEY` and `TSDK_DEPLOY_KEY` secrets.
Camera capture, projector alignment, audio, and latency still need testing on real hardware.

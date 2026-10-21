# TSDK: SDK for Serious Exergames

TSDK creates, checks, packages, and test-runs games for [TTEA](https://github.com/laurogripa/ttea), the interactive-floor exergame console.
Games run on TTEA's own runtime, tracking, and camera code.

## Start a game in six commands

Keep `ttea`, `tsdk`, and your games in one parent directory, then run:

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
| `tsdk new DEST [--id ID] [--name NAME] [--profile body\|hands\|face]` | Project with `ttea.toml`, `pyproject.toml`, `game.py`, `.gitignore`, and `LICENSE` |
| `tsdk check [project]` | Validates manifest and source without running the game |
| `tsdk dev [project]` | Opens developer mode |
| `tsdk build [project] [--output OUTPUT] [--release]` | Writes `dist/<id>-<version>.ttea` |

Commit each game's `uv.lock`. The `../` paths in `[tool.uv.sources]` assume this layout; change them for another one.

## A `.ttea` file is a game TTEA can install

A `.ttea` file is a ZIP archive that TTEA validates, installs, and runs. It holds:

- the entrypoint module, root Python modules, and top-level packages
- the assets and license notices listed in `ttea.toml`
- `manifest.json`, generated from `ttea.toml` at build time

You write `ttea.toml`. This one is from Ginga Divina:

```toml
[game]
id = "gingadivina"
version = "2.0.0"
entrypoint = "game.main:GingaDivina"
tracking_profile = "body"   # body, hands, or face
resolution = [800, 600]     # optional; default 800x600 (4:3)
game_api = 1
requires_ttea = ">=2.0,<3"
assets = ["assets"]
licenses = ["LICENSE", "CREDITS.md"]

[game.name]
pt = "Ginga Divina"
en = "Ginga Divina"
```

The build adds these provenance fields to `manifest.json`; games cannot set them:

| Field | Value |
| --- | --- |
| `format_version` | `1` |
| `builder.version` | Installed `tsdk` version |
| `build.source_commit` | Git `HEAD`, or `"unversioned"` outside Git |
| `build.dirty` | `true` with uncommitted or untracked files |

## Developer mode runs your game beside tracking tools

`uv run tsdk dev [project]` opens a full-screen window with no game loaded.
**Load game** browses from `project` (default: current directory) and accepts project folders or `.ttea` files.

- **Left two thirds**: the game, in TTEA's runtime.
- **Right third**: tracking source, skeleton preview, record and replay, and Load/Restart/Stop game (Exit once stopped).

| Tracking source | How it moves the player |
| --- | --- |
| Mouse | Drag the skeleton in the preview to move both feet |
| Keyboard | W A S D move the left foot; arrow keys move the right foot |
| Camera | **Both hands**: each palm moves one foot. **Face (nose)**: the nose moves the centre of both feet |

Mouse and Keyboard need no camera, player registration, or calibration. The webcam opens only when you select Camera.
Session results go to temporary storage.

> **Note:** The runtime renders off-screen, so `pygame.mouse.get_pos` and `pygame.key.get_pressed` stay idle. Read input from `context.events`.

### Registered player tests per-player settings

By default the game runs as an anonymous player, and `context.records` is `None`.
Check **Registered player** to get `context.records`, backed by one JSON file per game id:
`<data dir>/tsdk/players/<game id>.json`. The file survives restarts.
The data dir is `~/Library/Application Support` on macOS, `~/.local/share` on Linux, and `%LOCALAPPDATA%` on Windows.
The game's first registered start saves its defaults there.

With a game loaded and **Registered player** checked, **Edit config…** opens that configuration as JSON.
For example, set KarTEA's `"palette": 4` and `"horizon_image": "horizonnight"`.
Save stops the game, writes the file, and restarts it. Deleted keys fall back to the game's defaults.
**Reset** deletes the file, sessions included.

## Release builds need a clean Git checkout

Commit your changes, keep `dist/` in `.gitignore`, then run:

```sh
uv run tsdk build --release --output dist/game.ttea
```

Release builds fail on unversioned source, modified files, or untracked files.
Every packaged file must be tracked, including assets your `.gitignore` excludes.
The output path cannot overwrite packaged source, `ttea.toml`, or `pyproject.toml`.

## Checks enforce the TTEA game contract

`tsdk check` reads `ttea.toml` through `ttea.sdk.cartridge` and parses source without importing it.

- **Dependencies**: `pyproject.toml` lists only `t-tea`, with the same range as `requires_ttea`. Put tools in a development group.
- **Imports**: standard library, `pygame`, `numpy`, `ttea.sdk.api`, `ttea.sdk.tracking`, and the game's own modules.
- **Calls**: no display setup, event pumping, clock, camera, `subprocess`, `eval`/`exec`, or dependency installation.
- **Files**: no symlinks, hidden paths, compiled binaries, ML models, installers, or bundled platform libraries.
- **Lifecycle**: the entrypoint class defines synchronous `(self, context)` methods `start`, `update`, `draw`, `pause`, `resume`, and `stop`, or inherits them from `ttea.sdk.api.Game`.

These checks enforce conventions; they are not a security sandbox.

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
| `ttea-ref`, `builder-ref` | required | TTEA and TSDK refs; pin reviewed commits |
| `project-path` | `.` | Game directory inside the repository |
| `python-versions` | `'["3.14"]'` | JSON list of Python versions to test |
| `release` | `false` | Build with `--release` |

On Linux x86-64, Windows x86-64, and macOS ARM64, the workflow checks the game, runs its tests, and builds the cartridge.
It then installs and runs the cartridge outside the checkout, including synthetic tracking loss and recovery.
Private TTEA and TSDK repositories need the `TTEA_DEPLOY_KEY` and `TSDK_DEPLOY_KEY` secrets.
Camera capture, projector alignment, audio, and latency still need testing on real hardware.

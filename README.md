# TSDK: SDK for Serious Exergames

TSDK is the game platform for [TTEA](https://github.com/laurogripa/ttea), the interactive-floor exergame console.
It owns the game API, tracking, cartridge format, runtime, camera providers, simulator, and developer mode.
Games depend only on `tsdk`; TTEA runs their cartridges through `tsdk play`.

## Start a game in six commands

Keep `tsdk` and your games in one parent directory, then run:

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
| `tsdk play CARTRIDGE --lang LANG --player_id ID --professional_id ID --data-dir DIR` | Runs a cartridge for the TTEA console |

A new game's `pyproject.toml` declares one runtime dependency, `tsdk>=0.2,<0.3`, installed from GitHub through `[tool.uv.sources]`.
To develop against a local checkout, use `tsdk = { path = "../tsdk", editable = true }` there instead.
Commit each game's `uv.lock`.

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
requires_tsdk = ">=0.2,<0.3"
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

- **Left two thirds**: the game, in the TSDK runtime.
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

## TTEA plays cartridges with `tsdk play`

TTEA (branch `developer-mode`) lists `.ttea` files in its games folder and runs them with the `tsdk` command it finds on `PATH` or in `~/.local/bin`, so install tsdk once with `uv tool install --python 3.14 git+https://github.com/laurogripa/tsdk`. Each session starts from the folder holding the cartridge:

```sh
tsdk play game.ttea --lang pt_BR --player_id 7 --professional_id 3 --data-dir "<TTEA user data dir>"
```

`--data-dir` is TTEA's `platformdirs.user_data_dir("ttea", "udesc")`.
`tsdk play` validates the cartridge, including `requires_tsdk` against the installed `tsdk`, then runs it with the camera.
It reads TTEA's calibration without changing it:

| File in `<data-dir>/calibration/` | Used for |
| --- | --- |
| `calibration_hardware.ini` | Camera index (`[camera] camera_position`), game surface size (`[content]`) |
| `calibration_point.csv` | The four floor corners, which give the camera-to-game homography |

The corners are read as TTEA's own games read them: top-left, top-right, bottom-left, bottom-right, in pixels of the mirrored camera image. TTEA's calibration screens open the camera without a size request, so `tsdk play` does the same and measures that default frame size.
Game data stays under `<data-dir>/tsdk/<game id>/`: saves and results in `<player id>/`, registered-player records in `records/<player id>.json`.
The exit code is 0 after a normal quit and 1, with a message on stderr, for an invalid or incompatible cartridge or missing calibration.

## Release builds need a clean Git checkout

Commit your changes, keep `dist/` in `.gitignore`, then run:

```sh
uv run tsdk build --release --output dist/game.ttea
```

Release builds fail on unversioned source, modified files, or untracked files.
Every packaged file must be tracked, including assets your `.gitignore` excludes.
The output path cannot overwrite packaged source, `ttea.toml`, or `pyproject.toml`.

## Checks enforce the TTEA game contract

`tsdk check` reads `ttea.toml` through `tsdk.cartridge` and parses source without importing it.

- **Dependencies**: `pyproject.toml` lists only `tsdk`, with lower and upper bounds and the same range as `requires_tsdk`. Put tools in a development group.
- **Imports**: standard library, `pygame`, `numpy`, `tsdk.api`, `tsdk.tracking`, and the game's own modules. Every other `tsdk` module is platform-owned.
- **Calls**: no display setup, event pumping, clock, camera, `subprocess`, `eval`/`exec`, or dependency installation.
- **Files**: no symlinks, hidden paths, compiled binaries, ML models, installers, or bundled platform libraries.
- **Lifecycle**: the entrypoint class defines synchronous `(self, context)` methods `start`, `update`, `draw`, `pause`, `resume`, and `stop`, or inherits them from `tsdk.api.Game`.

These checks enforce conventions; they are not a security sandbox.

## Game repositories reuse one CI workflow

```yaml
jobs:
  tsdk:
    uses: laurogripa/tsdk/.github/workflows/game.yml@main
    with:
      builder-ref: main
    secrets: inherit
```

| Input | Default | Purpose |
| --- | --- | --- |
| `builder-ref` | required | TSDK ref; pin a reviewed commit |
| `project-path` | `.` | Game directory inside the repository |
| `python-versions` | `'["3.14"]'` | JSON list of Python versions to test |
| `release` | `false` | Build with `--release` |

On Linux x86-64, Windows x86-64, and macOS ARM64, the workflow checks the game, runs its tests, and builds the cartridge.
It then installs and runs the cartridge outside the checkout, including synthetic tracking loss and recovery.
A private TSDK repository needs the `TSDK_DEPLOY_KEY` secret.
Camera capture, projector alignment, audio, and latency still need testing on real hardware.

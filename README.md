# Develop and build portable TSDK games

TSDK validates games against the installed TTEA schema, builds `.ttea` ZIP cartridges,
and runs them in developer mode. Games always run on TTEA's runtime, tracking, and camera providers;
TSDK adds only the developer window and its synthetic controls.

## Develop with sibling repositories

Keep `ttea`, `TSDK`, and game checkouts in the same parent directory.

```sh
cd TSDK
uv sync --locked
uv run tsdk new ../example-game --profile body
cd ../example-game
uv lock
uv run tsdk check
uv run tsdk dev
uv run tsdk build
```

Commit each game's `uv.lock`. Adjust development source paths when using another directory layout.
The local source configuration is development-only; installed package metadata declares bounded registry dependencies.
No package publication or remote repository creation is performed by these commands.

## Run developer mode

`uv run tsdk dev [project]` opens a full-screen developer window with no game loaded;
Load game starts browsing from `project` (default: the current directory) and also accepts `.ttea` cartridges.
The game runs on the left two thirds in TTEA's runtime. The right third holds the tracking source,
the skeleton preview, recording/replay, and Load/Restart/Stop game (Exit once the game stops) buttons.

| Source | Drives the game with |
| --- | --- |
| Synthetic | Presets, joints, presence, and staleness edited in the panel |
| Mouse + keyboard | The mouse over the skeleton; hold A or D to plant the left or right foot |
| Camera tracking | Body; both index fingers pointing down or both whole hands placing the feet; or the nose as the centre of the feet |

Synthetic and mouse sources need no camera, player registration, or physical calibration.
Camera tracking opens the selected webcam only when chosen.

## Build a release

Commit game changes and exclude `dist/` in `.gitignore`, then run:

```sh
uv run tsdk build --release --output dist/game.ttea
```

Every build records `importlib.metadata.version('tsdk')`, the source Git commit, and dirty state.
Local unversioned builds record `source_commit = "unversioned"` and `dirty = true`.
Release builds reject unversioned, modified, and untracked source state.
Every packaged file and both authored metadata files must be tracked, including otherwise ignored assets.
Output paths cannot overwrite packaged source or authored metadata.
Only the installed builder determines provenance; game-authored provenance is not accepted.

## Understand the checks

The checker validates the authored manifest through `ttea.sdk.cartridge` without importing game code.
Runtime requirements contain only a bounded TTEA dependency matching the manifest; tooling belongs in development groups.
Root Python modules, top-level Python packages, declared assets, and license notices enter the cartridge.
Caches are omitted. Symlinks, binary extensions, models, installers, platform copies, and hidden payloads are rejected.

AST checks resolve imported and direct assignment aliases, reject unsupported imports and dynamic execution,
and prevent game-owned display initialization, event pumping, capture, detectors, and dependency installation.
Ordinary Pygame drawing and NumPy operations remain available.
Lifecycle callbacks may inherit the public `Game` defaults; overrides use synchronous instance methods with `(self, context)`.
Required keyword-only parameters and static/class methods are rejected because the runtime cannot call them.
Static checks enforce concrete conventions, not a security sandbox or proof against adversarial Python.

## Reuse native CI

`.github/workflows/game.yml` accepts `ttea-ref`, `builder-ref`, `project-path`, and `release`.
Pin the first two inputs to reviewed commits. The workflow installs both projects as packages,
checks game contracts and public boundaries, builds an artifact, then extracts and runs it outside the checkout.
Synthetic replay includes tracking loss and recovery. Windows x86-64, Linux x86-64, and macOS ARM64
run Python 3.10 and 3.12. Call the workflow only after remote creation/publication is authorized.
Physical capture, projector alignment, audio, and latency still require hardware acceptance.

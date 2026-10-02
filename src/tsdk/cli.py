import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from ttea.sdk.cartridge import inspect_cartridge, validate_manifest

from .checks import check
from .template import new_project


def provenance(root):
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"source_commit": "unversioned", "dirty": True}
    return {"source_commit": commit, "dirty": bool(status)}


def build(root, output=None, release=False):
    root = Path(root).resolve()
    manifest, files = check(root)
    source = provenance(root)
    if release and source["dirty"]:
        raise ValueError("Release builds require a clean committed Git checkout, including untracked files")
    if release:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True, capture_output=True,
        ).stdout.decode().split("\0")
        required = {path.relative_to(root).as_posix() for path in files} | {"ttea.toml", "pyproject.toml"}
        missing = required - set(tracked)
        if missing:
            raise ValueError("Release content must be committed: " + ", ".join(sorted(missing)))
    manifest.update(format_version=1, builder={"version": importlib.metadata.version("tsdk")}, build=source)
    manifest = validate_manifest(manifest, built=True)
    destination = Path(output).resolve() if output else root / "dist" / f"{manifest['id']}-{manifest['version']}.ttea"
    if destination in files or destination in {root / "ttea.toml", root / "pyproject.toml"}:
        raise ValueError("Output must not overwrite cartridge source content")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tsdk-build-", dir=destination.parent) as temporary:
        archive = Path(temporary) / "artifact.ttea"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            contents = [(path.relative_to(root).as_posix(), path.read_bytes()) for path in files]
            contents.append(("manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()))
            for name, data in sorted(contents):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                zipped.writestr(info, data)
        inspect_cartridge(archive)
        archive.replace(destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tsdk", description="Validate and build portable TTEA games")
    parser.add_argument("--version", action="version", version=importlib.metadata.version("tsdk"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "build"):
        command = commands.add_parser(name)
        command.add_argument("project", type=Path, nargs="?", default=Path.cwd())
        if name == "build":
            command.add_argument("--output", type=Path)
            command.add_argument("--release", action="store_true")
    new = commands.add_parser("new")
    new.add_argument("destination", type=Path)
    new.add_argument("--id", default="example-game")
    new.add_argument("--name", default="Example Game")
    new.add_argument("--profile", choices=("body", "hands", "face"), default="body")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            manifest, files = check(args.project)
            print(f"OK {manifest['id']} {manifest['version']}: {len(files)} portable files")
        elif args.command == "build":
            print(build(args.project, args.output, args.release))
        else:
            print(new_project(args.destination, args.id, args.name, args.profile))
    except (ValueError, OSError, SyntaxError) as error:
        print(f"tsdk: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Declarative TSDK metadata and bounded, portable ZIP extraction.

Cartridges contain trusted publisher Python, not sandboxed code. These checks
protect installation boundaries; they do not establish that gameplay is safe.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
import zlib

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

import tomllib

MAX_FILES = 10_000
MAX_FILE_SIZE = 128 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_MANIFEST_SIZE = 1024 * 1024
_FORBIDDEN_SUFFIXES = {
    '.pyc', '.pyo', '.pyd', '.so', '.dll', '.dylib', '.exe', '.com',
    '.whl', '.egg', '.pth', '.bat', '.cmd', '.ps1', '.sh', '.msi',
    '.tflite', '.task', '.onnx', '.pb', '.pt', '.h5', '.hdf5',
    '.bin', '.safetensors', '.keras', '.ckpt', '.weights', '.model',
    '.zip', '.tar', '.gz', '.7z',
    '.wasm', '.o', '.a', '.lib', '.class', '.jar',
}
_FORBIDDEN_PARTS = {
    '__pycache__', '.venv', 'venv', 'site-packages', 'dist-packages',
    'tsdk', 'pygame', 'mediapipe', 'numpy', 'cv2', 'pyside6', 'shiboken6',
    'pip', 'setuptools', 'ensurepip', 'python', 'python3',
}
_FORBIDDEN_NAMES = {
    'setup.py', 'setup.cfg', 'pyproject.toml', 'pipfile', 'pipfile.lock',
    'uv.lock', 'poetry.lock', 'environment.yml', 'environment.yaml',
    'sitecustomize.py', 'usercustomize.py',
}
_RESERVED = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$'} | {
    f'{prefix}{n}' for prefix in ('com', 'lpt') for n in (*range(1, 10), '¹', '²', '³')
}


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError('Content paths must be nonempty relative POSIX paths (max 240 characters)')
    if '\\' in value or value.startswith('/') or any(ord(c) < 32 for c in value):
        raise ValueError(f'Unsafe content path: {value!r}')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or ':' in p
           or any(c in p for c in '<>"|?*') or p.split('.')[0].casefold() in _RESERVED
           for p in parts):
        raise ValueError(f'Nonportable content path: {value!r}')
    return value


def _key(path: str) -> str:
    return unicodedata.normalize('NFC', path).casefold()


def _localized(value: object, label: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError(f'{label} must contain localized text')
    if any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*', k)
           or not isinstance(v, str) or not v.strip() for k, v in value.items()):
        raise ValueError(f'{label} must map language tags to nonempty strings')
    return dict(value)


def localized(texts: dict, language: str | None, default: str | None = None) -> str | None:
    """Pick a manifest text: exact BCP 47 tag, then its language subtag, then the first entry."""
    if not texts:
        return default
    tag = str(language or '').replace('_', '-')
    lowered = {key.lower(): value for key, value in texts.items()}
    for candidate in (tag.lower(), tag.split('-')[0].lower()):
        if candidate in lowered:
            return lowered[candidate]
    return next(iter(texts.values()))


def validate_manifest(manifest: dict, built: bool = False) -> dict:
    """Return normalized metadata, rejecting unsupported schema/runtime versions."""
    if not isinstance(manifest, dict):
        raise ValueError('Manifest must be an object')
    required = {'id', 'version', 'entrypoint', 'tracking_profile', 'game_api',
                'requires_tsdk', 'name', 'assets', 'licenses'}
    allowed = required | {'description', 'resolution'}
    if built:
        required |= {'format_version', 'builder', 'build'}
        allowed |= {'format_version', 'builder', 'build'}
    if required - manifest.keys() or manifest.keys() - allowed:
        raise ValueError(f'Manifest fields: missing {sorted(required - manifest.keys())}, '
                         f'unknown {sorted(manifest.keys() - allowed)}')
    result = dict(manifest)
    if not isinstance(result['id'], str) or not re.fullmatch(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*', result['id']):
        raise ValueError('id must be a lowercase portable game identifier')
    for field in ('version',):
        try:
            if not isinstance(result[field], str):
                raise InvalidVersion(str(result[field]))
            Version(result[field])
        except InvalidVersion as exc:
            raise ValueError(f'Invalid {field}') from exc
    if not isinstance(result['entrypoint'], str) or not re.fullmatch(
        r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*', result['entrypoint'], re.ASCII
    ):
        raise ValueError('entrypoint must be module:Class')
    if result['tracking_profile'] not in ('body', 'hands', 'face'):
        raise ValueError('Unsupported tracking_profile')
    if type(result['game_api']) is not int or result['game_api'] != 1:
        raise ValueError('Unsupported game_api; this runtime supports 1')
    try:
        requirement = result['requires_tsdk']
        if not isinstance(requirement, str) or not requirement.strip():
            raise InvalidSpecifier(str(requirement))
        specifier = SpecifierSet(requirement)
        current = importlib.metadata.version('tsdk')
        if not specifier.contains(current, prereleases=True):
            raise ValueError(f'Cartridge requires TSDK {requirement}; installed version is {current}')
    except InvalidSpecifier as exc:
        raise ValueError('Invalid requires_tsdk version range') from exc
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError('Install tsdk before validating cartridge compatibility') from exc
    result['name'] = _localized(result['name'], 'name')
    if 'description' in result:
        result['description'] = _localized(result['description'], 'description')
    if 'resolution' in result:
        resolution = result['resolution']
        if (not isinstance(resolution, list) or len(resolution) != 2
                or any(type(side) is not int or not 1 <= side <= 16384 for side in resolution)):
            raise ValueError('resolution must be [width, height] in pixels')
        result['resolution'] = list(resolution)
    for field in ('assets', 'licenses'):
        values = result[field]
        if not isinstance(values, list) or (field == 'licenses' and not values):
            raise ValueError(f'{field} must be a list; licenses must not be empty')
        result[field] = [_path(item) for item in values]
        if len({_key(item) for item in result[field]}) != len(values):
            raise ValueError(f'Duplicate {field} paths')
    if built:
        if type(result['format_version']) is not int or result['format_version'] != 1:
            raise ValueError('Unsupported format_version; this runtime supports 1')
        builder = result['builder']
        if not isinstance(builder, dict) or set(builder) != {'version'}:
            raise ValueError('builder must contain exactly version')
        try:
            if not isinstance(builder['version'], str):
                raise InvalidVersion(str(builder['version']))
            Version(builder['version'])
        except InvalidVersion as exc:
            raise ValueError('builder.version must be an exact version') from exc
        build = result['build']
        if (not isinstance(build, dict) or set(build) != {'source_commit', 'dirty'}
                or not isinstance(build['source_commit'], str)
                or type(build['dirty']) is not bool
                or not (re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', build['source_commit'])
                        or (build['source_commit'] == 'unversioned' and build['dirty']))):
            raise ValueError('build requires a full source_commit hash (or unversioned when dirty) and boolean dirty')
        result['builder'] = dict(builder)
        result['build'] = dict(build)
    return result


def _entrypoints(manifest: dict) -> tuple[str, str]:
    module = manifest['entrypoint'].split(':')[0].replace('.', '/')
    return module + '.py', module + '/__init__.py'


def read_project(path: str | Path) -> dict:
    """Read the single authored ttea.toml without importing game code."""
    path = Path(path)
    metadata = path / 'ttea.toml' if path.is_dir() else path
    if metadata.is_symlink() or not metadata.is_file():
        raise ValueError('ttea.toml must be a regular file')
    if metadata.stat().st_size > MAX_MANIFEST_SIZE:
        raise ValueError('ttea.toml is too large')
    with metadata.open('rb') as stream:
        document = tomllib.load(stream)
    if set(document) != {'game'}:
        raise ValueError('ttea.toml must contain exactly the game table')
    manifest = validate_manifest(document['game'])
    root = metadata.parent.resolve()
    for name in manifest['assets'] + manifest['licenses']:
        target = root / name
        if any(part.is_symlink() for part in [target, *target.parents] if part != root and root in part.parents):
            raise ValueError(f'Project content cannot be a link: {name}')
        if not target.exists() or not target.resolve().is_relative_to(root):
            raise ValueError(f'Missing project content: {name}')
        if name in manifest['licenses'] and (not target.is_file() or target.stat().st_size == 0):
            raise ValueError(f'License must be a nonempty file: {name}')
    if not any((root / name).is_file() and not (root / name).is_symlink() for name in _entrypoints(manifest)):
        raise ValueError('Entrypoint Python source is missing')
    return manifest


def _content_name(name: str) -> None:
    parts = PurePosixPath(name).parts
    lower = [part.casefold() for part in parts]
    leaf = lower[-1]
    if (any(part in _FORBIDDEN_PARTS or part.endswith(('.dist-info', '.egg-info')) for part in lower)
            or leaf in _FORBIDDEN_NAMES or leaf.startswith('requirements')
            or (leaf.endswith('.py') and leaf[:-3] in _FORBIDDEN_PARTS)
            or PurePosixPath(leaf).suffix in _FORBIDDEN_SUFFIXES
            or re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', leaf)):
        raise ValueError(f'Prohibited cartridge content: {name}')


def _object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate manifest key: {key}')
        result[key] = value
    return result


def _inspect(archive: zipfile.ZipFile) -> tuple[dict, list[zipfile.ZipInfo]]:
    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise ValueError('Cartridge has too many entries')
    names = {}
    files = {}
    total = 0
    for entry in entries:
        if entry.orig_filename != entry.filename:
            raise ValueError('ZIP filename contains a NUL byte or backslash')
        if entry.is_dir() and entry.file_size:
            raise ValueError('Directory entries cannot contain data')
        name = _path(entry.filename[:-1] if entry.is_dir() else entry.filename)
        key = _key(name)
        if key in names:
            raise ValueError(f'Colliding cartridge path: {name}')
        names[key] = (name, entry.is_dir())
        mode = entry.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG):
            raise ValueError(f'Links and special files are prohibited: {name}')
        if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError('Encrypted or unsupported ZIP compression')
        if entry.file_size > MAX_FILE_SIZE or entry.file_size > max(entry.compress_size, 1) * MAX_COMPRESSION_RATIO:
            raise ValueError(f'Cartridge entry exceeds size/ratio limit: {name}')
        total += entry.file_size
        if total > MAX_TOTAL_SIZE:
            raise ValueError('Cartridge exceeds total uncompressed size limit')
        _content_name(name)
        if not entry.is_dir():
            files[name] = entry
    # Check implicit directories too, including spelling/case aliases on Windows/macOS.
    directories = {}
    for name, is_dir in names.values():
        parts = name.split('/')
        for i in range(1, len(parts) + (1 if is_dir else 0)):
            prefix = '/'.join(parts[:i])
            key = _key(prefix)
            if key in directories and directories[key] != prefix:
                raise ValueError(f'Colliding directory spelling: {prefix}')
            directories[key] = prefix
            if key in names and (not names[key][1] or names[key][0] != prefix):
                raise ValueError(f'File/directory collision: {prefix}')
    if 'manifest.json' not in files or files['manifest.json'].file_size > MAX_MANIFEST_SIZE:
        raise ValueError('A bounded root manifest.json is required')
    manifest = validate_manifest(json.loads(archive.read(files['manifest.json']), object_pairs_hook=_object), built=True)
    if not any(name in files for name in _entrypoints(manifest)):
        raise ValueError('Entrypoint Python source is missing')
    for name in manifest['assets']:
        if name not in files and not any(item.startswith(name + '/') for item in files):
            raise ValueError(f'Declared asset is missing: {name}')
    for name in manifest['licenses']:
        if name not in files or files[name].file_size == 0:
            raise ValueError(f'Declared license must be a nonempty file: {name}')
    for name, entry in files.items():
        with archive.open(entry) as stream:
            prefix = stream.read(16)
            if (prefix.startswith((b'MZ', b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                                   b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe'))
                    or prefix[4:8] == b'TFL3'
                    or prefix[:4] in (b'\xca\xfe\xba\xbf', b'\xbe\xba\xfe\xca', b'\xbf\xba\xfe\xca')
                    or (len(prefix) >= 4 and prefix[2:4] == b'\r\n' and prefix[0] >= 128)):
                raise ValueError(f'Native executable, bytecode, or model content: {name}')
            consumed = len(prefix)
            while chunk := stream.read(1024 * 1024):
                consumed += len(chunk)
                if consumed > entry.file_size or consumed > MAX_FILE_SIZE:
                    raise ValueError(f'Expanded entry exceeds declared size: {name}')
            if consumed != entry.file_size:
                raise ValueError(f'Incorrect uncompressed size: {name}')
    return manifest, entries


def inspect_cartridge(path: str | Path) -> dict:
    """Validate all metadata and content without extracting or executing anything."""
    try:
        with zipfile.ZipFile(path) as archive:
            manifest, _ = _inspect(archive)
            return manifest
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, NotImplementedError, RuntimeError, zlib.error, EOFError) as exc:
        raise ValueError(f'Invalid cartridge: {exc}') from exc


def extract_cartridge(path: str | Path, destination: str | Path) -> dict:
    """Validate first, then publish a fully extracted tree to an absent/empty path.

    No destination or staging tree is modified until the complete archive passes
    validation. A private sibling tree prevents partial extraction on failure.
    """
    destination = Path(destination)
    try:
        with zipfile.ZipFile(path) as archive:
            manifest, entries = _inspect(archive)
            if destination.is_symlink() or (destination.exists() and (
                not destination.is_dir() or any(destination.iterdir())
            )):
                raise ValueError('Extraction destination must be absent or an empty directory')
            parent = destination.parent
            if not parent.is_dir():
                raise ValueError('Extraction destination parent must already exist')
            temporary = Path(tempfile.mkdtemp(prefix='.ttea-extract-', dir=parent))
            try:
                for entry in entries:
                    target = temporary / entry.filename
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as source, target.open('xb') as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                if destination.exists():
                    destination.rmdir()
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return manifest
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, NotImplementedError, RuntimeError, zlib.error, EOFError) as exc:
        raise ValueError(f'Invalid cartridge: {exc}') from exc

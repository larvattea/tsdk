"""Static compliance checks, not a Python security sandbox."""
import ast
from pathlib import Path
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import tomllib

from ttea.sdk.cartridge import read_project

FORBIDDEN_ROOTS = {"cv2", "mediapipe", "pip", "ensurepip", "subprocess", "ctypes", "cffi", "importlib"}
FORBIDDEN_CALLS = {
    "pygame.init", "pygame.quit", "pygame.display", "pygame.event",
    "pygame.camera", "pygame.mixer.init", "pygame.mixer.quit",
    "pygame.time.Clock", "os.system", "os.popen", "os.spawn", "os.exec",
    "__import__", "eval", "exec", "compile", "getattr",
    "ttea.sdk.runtime", "ttea.sdk.providers", "ttea.sdk.simulator",
}
PUBLIC_PLATFORM_MODULES = {"ttea.sdk.api", "ttea.sdk.tracking"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".pyd", ".whl", ".tflite", ".task", ".onnx", ".pb", ".pt", ".pth"}
FORBIDDEN_NAMES = {"setup.py", "setup.cfg", "pyproject.toml", "requirements.txt", "sitecustomize.py", "usercustomize.py", "manifest.json", "ttea.toml"}


def project_files(root, manifest):
    module = manifest["entrypoint"].split(":")[0]
    top = module.split(".")[0]
    source = root / top
    if not source.is_dir():
        source = root / (top + ".py")
    selected = [source] + [root / p for p in manifest["assets"] + manifest["licenses"]]
    selected += list(root.glob("*.py"))
    selected += [path.parent for path in root.glob("*/__init__.py") if not path.parent.name.startswith(".")]
    files = set()
    for item in selected:
        if not item.exists():
            raise ValueError(f"Missing cartridge content: {item}")
        for path in ([item] if not item.is_dir() else [item, *item.rglob("*")]):
            if path.is_symlink():
                raise ValueError(f"Symbolic links are not portable cartridge content: {path}")
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.name == ".DS_Store":
                continue
            if any(part.startswith(".") or part in {"__pycache__", "node_modules", "venv", "site-packages"} for part in relative.parts):
                raise ValueError(f"Forbidden cartridge path: {relative}")
            if path.is_dir():
                continue
            if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.lower() in FORBIDDEN_NAMES:
                raise ValueError(f"Forbidden cartridge content: {relative}")
            if relative.parts[0] in {"ttea", "pygame", "numpy", "cv2", "mediapipe", "PySide6"}:
                raise ValueError(f"Bundled platform library: {relative}")
            files.add(path)
    return sorted(files)


def resolve_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = resolve_name(node.value, aliases)
        return f"{base}.{node.attr}" if base else ""
    return ""


def check_source(path, local_modules):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = {}
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                imports.append((node, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            for alias in node.names:
                if alias.name == "*":
                    raise ValueError(f"{path}:{node.lineno}: wildcard imports prevent ownership checks")
                name = f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name] = name
                imports.append((node, name))
    allowed = set(sys.stdlib_module_names) | {"pygame", "numpy", "ttea"} | local_modules
    for node, name in imports:
        top = name.split(".")[0]
        if top in FORBIDDEN_ROOTS or top not in allowed:
            raise ValueError(f"{path}:{node.lineno}: unsupported runtime import {name}")
        if top == "ttea" and not any(name == public or name.startswith(public + ".") for public in PUBLIC_PLATFORM_MODULES):
            raise ValueError(f"{path}:{node.lineno}: platform-owned import {name}")
    # Resolve direct aliases as well as imported aliases, including assignments
    # declared after their use inside a callback.
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = resolve_name(node.value, aliases)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = value
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = resolve_name(node.func, aliases)
            if any(name == forbidden or name.startswith(forbidden + ".") or (forbidden in {"os.spawn", "os.exec"} and name.startswith(forbidden)) for forbidden in FORBIDDEN_CALLS):
                raise ValueError(f"{path}:{node.lineno}: platform-owned or dynamic call {name}")
    return tree


def check(root):
    root = Path(root).resolve()
    manifest = read_project(root)
    config_path = root / "pyproject.toml"
    if not config_path.is_file():
        raise ValueError("Game project requires pyproject.toml with a bounded t-tea runtime dependency")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    project = config.get("project", {})
    dependencies = project.get("dependencies", [])
    if project.get("dynamic"):
        raise ValueError("Dynamic project metadata is not supported")
    if len(dependencies) != 1:
        raise ValueError("Game runtime dependencies must contain only t-tea; put tsdk in a development group")
    dependency = Requirement(dependencies[0])
    if canonicalize_name(dependency.name) != "t-tea" or dependency.url or dependency.extras or dependency.marker:
        raise ValueError("Only an unconditional t-tea version range is allowed")
    operators = {item.operator for item in dependency.specifier}
    if not operators.intersection({">=", ">", "==", "~="}) or not operators.intersection({"<", "<=", "==", "~="}):
        raise ValueError("t-tea dependency must have lower and upper version bounds")
    if str(dependency.specifier) != str(Requirement("t-tea" + manifest["requires_ttea"]).specifier):
        raise ValueError("pyproject.toml and ttea.toml must declare the same TTEA compatibility range")
    if project.get("optional-dependencies"):
        raise ValueError("Game-specific optional runtime dependencies are not supported")
    files = project_files(root, manifest)
    locals_ = {p.relative_to(root).parts[0].removesuffix(".py") for p in files if p.suffix == ".py"}
    trees = {path: check_source(path, locals_) for path in files if path.suffix == ".py"}
    module, class_name = manifest["entrypoint"].split(":")
    entry = root.joinpath(*module.split(".")).with_suffix(".py")
    if entry not in trees:
        entry = root.joinpath(*module.split("."), "__init__.py")
    tree = trees.get(entry)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name] if tree else []
    if len(classes) != 1:
        raise ValueError("Entrypoint must name a class defined in the packaged module")
    methods = {node.name: node for node in classes[0].body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    entry_aliases = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "ttea.sdk.api":
            for alias in node.names:
                entry_aliases[alias.asname or alias.name] = f"ttea.sdk.api.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                entry_aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
    inherits_game = any(resolve_name(base, entry_aliases) == "ttea.sdk.api.Game" for base in classes[0].bases)
    for name in ("start", "update", "draw", "pause", "resume", "stop"):
        method = methods.get(name)
        if method is None and inherits_game:
            continue
        if (
            method is None
            or isinstance(method, ast.AsyncFunctionDef)
            or len(method.args.posonlyargs + method.args.args) != 2
            or method.args.vararg
            or method.args.kwarg
            or any(default is None for default in method.args.kw_defaults)
            or any(resolve_name(decorator, entry_aliases) in {"staticmethod", "classmethod"}
                   for decorator in method.decorator_list)
        ):
            raise ValueError(f"Entrypoint requires {name}(self, context)")
    return manifest, files

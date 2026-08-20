"""Read the commands that a project defines without running project code."""

import json
import re
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml
from docker.client import DockerClient
from docker.errors import DockerException

from app.containers.hardened import Capture, Egress, HardenedRunSpec, run_hardened

MANIFEST_FILES = (
    "package.json",
    "Makefile",
    "makefile",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
)

# Read for their name, not their contents. A lockfile says which package
# manager the project actually uses, which is the difference between `npm test`
# and `pnpm test` — the most common way a proposed command is wrong.
LOCKFILES: tuple[tuple[str, str], ...] = (
    ("package-lock.json", "npm"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
)
CI_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")

MAX_FILE_BYTES = 200_000
MAX_LOG_BYTES = 16 * 1_048_576
MAX_CI_COMMANDS = 30
MAX_DEPENDENCIES = 60
_SECTION = "@@@FILE:"
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:(?!=)", re.MULTILINE)

_PYTHON_TOOLS = frozenset(
    {"pytest", "ruff", "mypy", "black", "flake8", "tox", "coverage"}
)
_PYTHON_RUNNERS = frozenset({"uv", "poetry", "pdm", "hatch", "python", "python3"})
_NODE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_NODE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
# Setup and publish steps are real CI commands but not the build, test, or lint
# commands an implementer needs, and they crowd out the ones that are.
_CI_NOISE = re.compile(
    r"^(cd |echo |export |git |gh |aws |docker |curl |wget |apt|sudo |"
    r"npm (ci|install|publish)|pnpm (install|publish)|yarn install|"
    r"pip install|uv sync|poetry install)",
)


@dataclass(frozen=True)
class CommandInventory:
    npm_scripts: frozenset[str] = field(default_factory=frozenset)
    make_targets: frozenset[str] = field(default_factory=frozenset)
    python_project: bool = False
    node_project: bool = False
    rust_project: bool = False
    go_project: bool = False
    #: The package manager a lockfile proves this project uses, if any.
    package_manager: str | None = None
    #: Commands this project's CI workflows actually run, in file order.
    ci_commands: tuple[str, ...] = ()
    #: (name, version specifier) pairs, so the turn reasons about the versions
    #: the project pins rather than whichever release it remembers.
    dependencies: tuple[tuple[str, str], ...] = ()

    @property
    def node_runner(self) -> str:
        """The command prefix for a package script. `npm` only if unproven."""
        if self.package_manager in _NODE_MANAGERS:
            return self.package_manager
        return "npm"

    def as_dict(self) -> dict[str, object]:
        return {
            "npm_scripts": sorted(self.npm_scripts),
            "make_targets": sorted(self.make_targets),
            "python_project": self.python_project,
            "node_project": self.node_project,
            "rust_project": self.rust_project,
            "go_project": self.go_project,
            "package_manager": self.package_manager,
            "ci_commands": list(self.ci_commands),
            "dependencies": [list(pair) for pair in self.dependencies],
        }


def confirm_command(command: str, inventory: CommandInventory) -> tuple[bool, str]:
    """Confirm that a command belongs to a detected project ecosystem."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False, "command is not parseable as a shell command"
    if not tokens:
        return False, "command is empty"

    program, arguments = tokens[0], tokens[1:]

    if program in _NODE_RUNNERS:
        if not inventory.node_project:
            return (
                False,
                f"'{program}' needs a package.json, which this project has none of",
            )
        proven = inventory.package_manager
        if proven in _NODE_MANAGERS and program != proven:
            return (
                False,
                (
                    f"this project's lockfile is {proven}'s, so '{program}' is the "
                    f"wrong package manager; use '{proven}'"
                ),
            )
        script = _node_script(program, arguments)
        if script is None:
            return False, f"could not tell which script '{command}' runs"
        if script in inventory.npm_scripts:
            return True, f"package.json defines the '{script}' script"
        return False, f"package.json defines no '{script}' script"

    if program == "make":
        target = arguments[0] if arguments else ""
        if not inventory.make_targets:
            return False, "this project has no Makefile"
        if not target:
            return False, "'make' without a target cannot be confirmed"
        if target in inventory.make_targets:
            return True, f"the Makefile defines the '{target}' target"
        return False, f"the Makefile defines no '{target}' target"

    if program in _PYTHON_TOOLS:
        if inventory.python_project:
            return True, f"'{program}' is a Python tool and this is a Python project"
        return False, f"'{program}' needs a Python project, which this is not"

    if program in _PYTHON_RUNNERS:
        if inventory.python_project:
            return True, f"'{program}' runs in this Python project"
        return False, f"'{program}' needs a Python project, which this is not"

    if program == "cargo":
        return (
            (True, "this is a Rust project")
            if inventory.rust_project
            else (False, "'cargo' needs a Cargo.toml, which this project has none of")
        )

    if program == "go":
        return (
            (True, "this is a Go project")
            if inventory.go_project
            else (False, "'go' needs a go.mod, which this project has none of")
        )

    return False, f"'{program}' is not a command this project is known to define"


def discover_inventory(
    docker_client: DockerClient,
    *,
    image: str,
    volume_name: str,
    timeout_seconds: int = 60,
) -> CommandInventory:
    return parse_inventory(
        _read_manifests(
            docker_client,
            image=image,
            volume_name=volume_name,
            timeout_seconds=timeout_seconds,
        )
    )


def parse_inventory(files: dict[str, str]) -> CommandInventory:
    """Build an inventory from manifest text, tolerating malformed files."""
    npm_scripts: set[str] = set()
    node_project = "package.json" in files
    if node_project:
        try:
            package = json.loads(files["package.json"])
            scripts = package.get("scripts")
            if isinstance(scripts, dict):
                npm_scripts = {str(name) for name in scripts}
        except (ValueError, AttributeError):
            npm_scripts = set()

    make_targets: set[str] = set()
    for name in ("Makefile", "makefile"):
        if name in files:
            make_targets |= {
                match.group(1)
                for match in _MAKE_TARGET.finditer(files[name])
                if not match.group(1).startswith(".")
            }

    python_project = "pyproject.toml" in files
    if python_project:
        try:
            tomllib.loads(files["pyproject.toml"])
        except tomllib.TOMLDecodeError:
            pass

    return CommandInventory(
        npm_scripts=frozenset(npm_scripts),
        make_targets=frozenset(make_targets),
        python_project=python_project,
        node_project=node_project,
        rust_project="Cargo.toml" in files,
        go_project="go.mod" in files,
        package_manager=_package_manager(files),
        ci_commands=_ci_commands(files),
        dependencies=_dependencies(files),
    )


def _package_manager(files: Mapping[str, str]) -> str | None:
    """The first lockfile present wins, node before python.

    A repository with both a node and a python lockfile has two, and the node
    one is reported: `commands` is dominated by package scripts when a
    package.json exists. `confirm_command` still checks python tools against
    `python_project`, so nothing is lost by not reporting the second.
    """
    for name, manager in LOCKFILES:
        if name in files:
            return manager
    return None


def _ci_commands(files: Mapping[str, str]) -> tuple[str, ...]:
    """The `run:` steps of every GitHub workflow, minus setup and publish noise.

    CI is the strongest evidence a repository carries about how it is built and
    tested: those commands demonstrably pass on a clean checkout. Parsed with a
    real YAML loader, because `run: |` blocks are the interesting case and a
    regex reads their continuation lines as separate keys.
    """
    found: list[str] = []
    for name, text in files.items():
        if not name.startswith(".github/workflows/"):
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        for command in _run_steps(document):
            for line in command.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if _CI_NOISE.match(stripped):
                    continue
                if stripped not in found:
                    found.append(stripped)
    return tuple(found[:MAX_CI_COMMANDS])


def _run_steps(node: Any) -> list[str]:
    """Every `run:` string anywhere in a workflow document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_run_steps(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_run_steps(item))
    return found


def _dependencies(files: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Declared dependencies with the version each manifest pins.

    Names alone would not help: the turn already knows what `fastapi` is. What
    it cannot know is which release this project is on, which is what decides
    whether the API it recommends exists here.
    """
    found: list[tuple[str, str]] = []
    if "package.json" in files:
        try:
            package = json.loads(files["package.json"])
        except ValueError:
            package = {}
        if isinstance(package, dict):
            for section in ("dependencies", "devDependencies"):
                declared = package.get(section)
                if isinstance(declared, dict):
                    found.extend(
                        (str(name), str(version)) for name, version in declared.items()
                    )
    if "pyproject.toml" in files:
        try:
            project = tomllib.loads(files["pyproject.toml"]).get("project", {})
        except tomllib.TOMLDecodeError:
            project = {}
        declared = project.get("dependencies") if isinstance(project, dict) else None
        if isinstance(declared, list):
            found.extend(_requirement(str(item)) for item in declared)
    return tuple(found[:MAX_DEPENDENCIES])


def _requirement(specifier: str) -> tuple[str, str]:
    """Split `fastapi>=0.115,<1.0` into its name and the rest."""
    match = re.match(r"^\s*([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$", specifier)
    if match is None:
        return specifier.strip(), ""
    return match.group(1), match.group(3).strip()


def _node_script(program: str, arguments: list[str]) -> str | None:
    if not arguments:
        return None
    if arguments[0] == "run":
        return arguments[1] if len(arguments) > 1 else None
    if arguments[0] in {"install", "ci", "exec", "dlx", "add"}:
        return None
    return arguments[0]


def _read_manifests(
    docker_client: DockerClient,
    *,
    image: str,
    volume_name: str,
    timeout_seconds: int,
) -> dict[str, str]:
    script = "cd /workspace\n"
    script += "".join(
        f"if [ -f {name} ]; then printf '{_SECTION}{name}\\n'; "
        f"head -c {MAX_FILE_BYTES} {name}; printf '\\n'; fi\n"
        for name in MANIFEST_FILES
    )
    # Lockfiles are announced, not read. Their name is the whole signal, and
    # one of them is a multi-megabyte binary.
    script += "".join(
        f"if [ -f {name} ]; then printf '{_SECTION}{name}\\n\\n'; fi\n"
        for name, _ in LOCKFILES
    )
    script += "".join(
        f"for f in {pattern}; do "
        f'[ -f "$f" ] || continue; printf \'{_SECTION}%s\\n\' "$f"; '
        f"head -c {MAX_FILE_BYTES} \"$f\"; printf '\\n'; done\n"
        for pattern in CI_GLOBS
    )
    try:
        result = run_hardened(
            docker_client,
            HardenedRunSpec(
                image=image,
                command=["sh", "-c", script],
                entrypoint=[],
                egress=Egress.DENIED,
                working_dir="/workspace",
                environment={"HOME": "/tmp/home"},
                labels={
                    "orchestrator.managed": "true",
                    "orchestrator.kind": "inventory",
                },
                volumes={volume_name: {"bind": "/workspace", "mode": "ro"}},
                tmpfs_size="16m",
                timeout_seconds=timeout_seconds,
                max_log_bytes=MAX_LOG_BYTES,
                capture=Capture.SEPARATE,
            ),
        )
        if result.timed_out:
            return {}
        stdout = result.stdout
    except DockerException:
        return {}
    return _split(stdout)


def _split(stdout: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for block in stdout.split(_SECTION):
        if not block.strip():
            continue
        name, _, body = block.partition("\n")
        files[name.strip()] = body
    return files

"""Guard the app import direction until the known cycle is removed.

The node set is every app-level *module*, not only directory packages.
Root-level ``.py`` modules were invisible to an earlier version of this
test, which is why a real ``previews`` <-> ``dependency_cache`` cycle went
unreported until Phase 9 widened the node set and then cut it.

`app/main.py` is the only root-level module left, so the wider node set
currently catches nothing extra. Keep it anyway: it is what stops the next
root-level module from re-opening the same blind spot.
"""

import ast
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

# This known-bad baseline must shrink as later architecture phases land.
KNOWN_CYCLE = frozenset(
    {
        "previews",
        "sandboxes",
    }
)
MODULES_OUTSIDE_KNOWN_CYCLE = frozenset(
    {
        "agents",
        "containers",
        "controller",
        "delegation",
        "implementation_context",
        "planning",
        "platform",
        "projects",
        "startup",
        "tasks",
        "turns",
        "volumes",
    }
)


def _app_root() -> Path:
    return Path(__file__).resolve().parents[1] / "app"


def _module_parts(path: Path, app_root: Path) -> tuple[str, ...]:
    parts = path.relative_to(app_root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ("app", *parts)


def _import_targets(
    node: ast.Import | ast.ImportFrom, package: tuple[str, ...]
) -> Iterable[tuple[str, ...]]:
    if isinstance(node, ast.Import):
        return [tuple(alias.name.split(".")) for alias in node.names]
    if node.level:
        prefix = package[: len(package) - (node.level - 1)]
    else:
        prefix = ()
    module = tuple(node.module.split(".")) if node.module else ()
    base = prefix + module
    # `from app import sandboxes` names the package in the alias, not the
    # module, so the alias has to be appended or the edge is invisible.
    return [base, *(base + (alias.name,) for alias in node.names)]


def _app_nodes(app_root: Path) -> set[str]:
    """Every app-level node: directory packages plus root-level modules.

    Restricting this to directory packages hides any cycle that runs through
    a root-level module such as ``app/dependency_cache.py``.
    """
    packages = {
        child.name
        for child in app_root.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }
    modules = {
        child.stem for child in app_root.glob("*.py") if child.stem != "__init__"
    }
    return packages | modules


def _package_import_graph(app_root: Path) -> dict[str, set[str]]:
    packages = _app_nodes(app_root)
    graph: dict[str, set[str]] = defaultdict(
        set, {package: set() for package in packages}
    )
    for path in app_root.rglob("*.py"):
        module = _module_parts(path, app_root)
        if len(module) < 2:
            continue
        source = module[1]
        if source not in packages:
            continue
        package = module if path.stem == "__init__" else module[:-1]
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_targets(node, package):
                if (
                    len(target) > 1
                    and target[0] == "app"
                    and target[1] in packages
                    and target[1] != source
                ):
                    graph[source].add(target[1])
    return graph


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[frozenset[str]]:
    nodes = set(graph).union(*graph.values())
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[frozenset[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph[node]:
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] != indices[node]:
            return
        component: set[str] = set()
        while True:
            neighbor = stack.pop()
            on_stack.remove(neighbor)
            component.add(neighbor)
            if neighbor == node:
                break
        components.append(frozenset(component))

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return components


def test_app_import_cycle_stays_at_the_known_baseline() -> None:
    components = _strongly_connected_components(_package_import_graph(_app_root()))
    largest = max(components, key=len)

    assert MODULES_OUTSIDE_KNOWN_CYCLE.isdisjoint(largest), (
        "Modules outside the known cycle joined it: "
        f"{sorted(MODULES_OUTSIDE_KNOWN_CYCLE.intersection(largest))}"
    )
    if largest < KNOWN_CYCLE:
        raise AssertionError(
            "The app import cycle shrank to "
            f"{sorted(largest)}. Tighten KNOWN_CYCLE to {sorted(largest)}."
        )
    if KNOWN_CYCLE < largest:
        raise AssertionError(
            "The app import cycle grew from "
            f"{sorted(KNOWN_CYCLE)} to {sorted(largest)}."
        )
    assert largest == KNOWN_CYCLE, (
        "The largest app import cycle changed unexpectedly: "
        f"expected {sorted(KNOWN_CYCLE)}, got {sorted(largest)}."
    )

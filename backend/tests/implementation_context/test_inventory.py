import json

import pytest

from app.implementation_context.inventory import (
    CommandInventory,
    confirm_command,
    parse_inventory,
)

NODE = {
    "package.json": json.dumps(
        {"scripts": {"build": "astro build", "test": "vitest", "lint": "eslint ."}}
    )
}
PYTHON = {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'}
MAKE = {
    "Makefile": "build:\n\tgo build\n\ntest-unit: build\n\tgo test ./...\n\n.PHONY: build\n"
}


def test_npm_scripts_are_read_from_package_json() -> None:
    inventory = parse_inventory(NODE)

    assert inventory.node_project
    assert inventory.npm_scripts == {"build", "test", "lint"}


def test_malformed_package_json_yields_no_scripts() -> None:
    inventory = parse_inventory({"package.json": "{not json"})

    assert inventory.node_project
    assert inventory.npm_scripts == frozenset()


def test_make_targets_are_read_and_dot_targets_are_ignored() -> None:
    inventory = parse_inventory(MAKE)

    assert inventory.make_targets == {"build", "test-unit"}


def test_project_kinds_are_detected() -> None:
    assert parse_inventory(PYTHON).python_project
    assert parse_inventory({"Cargo.toml": "[package]"}).rust_project
    assert parse_inventory({"go.mod": "module x"}).go_project
    assert not parse_inventory({}).node_project


@pytest.mark.parametrize(
    "command",
    ["npm run build", "npm test", "pnpm run lint", "yarn build", "bun run test"],
)
def test_node_commands_matching_a_script_are_confirmed(command: str) -> None:
    confirmed, reason = confirm_command(command, parse_inventory(NODE))

    assert confirmed, reason


def test_unknown_node_script_is_refused() -> None:
    confirmed, reason = confirm_command("npm run test:unit", parse_inventory(NODE))

    assert not confirmed
    assert "no 'test:unit' script" in reason


def test_install_commands_are_not_treated_as_scripts() -> None:
    confirmed, _reason = confirm_command("npm ci", parse_inventory(NODE))

    assert not confirmed


def test_make_targets_are_confirmed_and_unknown_targets_are_refused() -> None:
    inventory = parse_inventory(MAKE)

    assert confirm_command("make build", inventory)[0]
    assert not confirm_command("make deploy", inventory)[0]
    assert not confirm_command("make", inventory)[0]


def test_python_tools_need_a_python_project() -> None:
    assert confirm_command("pytest -q", parse_inventory(PYTHON))[0]
    assert confirm_command("ruff check .", parse_inventory(PYTHON))[0]
    assert not confirm_command("pytest", parse_inventory(NODE))[0]


def test_cargo_and_go_need_their_manifests() -> None:
    assert confirm_command("cargo test", parse_inventory({"Cargo.toml": ""}))[0]
    assert not confirm_command("cargo test", parse_inventory(NODE))[0]
    assert confirm_command("go test ./...", parse_inventory({"go.mod": ""}))[0]


def test_unknown_unparseable_and_empty_commands_are_refused() -> None:
    confirmed, reason = confirm_command(
        "./scripts/run-everything.sh",
        parse_inventory(NODE),
    )

    assert not confirmed
    assert "not a command this project is known to define" in reason
    assert not confirm_command('npm run "unterminated', CommandInventory())[0]
    assert not confirm_command("   ", CommandInventory())[0]


WORKFLOW = """
name: CI
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pnpm install --frozen-lockfile
      - run: pnpm run lint
      - name: Test and build
        run: |
          pnpm run test -- --coverage
          pnpm run build
"""


def test_lockfile_names_the_package_manager() -> None:
    assert parse_inventory({**NODE, "pnpm-lock.yaml": ""}).package_manager == "pnpm"
    assert parse_inventory({**NODE, "bun.lockb": ""}).package_manager == "bun"
    assert parse_inventory(PYTHON | {"uv.lock": ""}).package_manager == "uv"
    assert parse_inventory(NODE).package_manager is None


def test_node_runner_falls_back_to_npm_only_when_unproven() -> None:
    assert parse_inventory(NODE).node_runner == "npm"
    assert parse_inventory({**NODE, "yarn.lock": ""}).node_runner == "yarn"


def test_the_wrong_package_manager_is_refused_even_for_a_real_script() -> None:
    inventory = parse_inventory({**NODE, "pnpm-lock.yaml": ""})

    confirmed, reason = confirm_command("npm run build", inventory)

    assert not confirmed
    assert "wrong package manager" in reason
    assert confirm_command("pnpm run build", inventory)[0]


def test_ci_run_steps_are_collected_including_block_scalars() -> None:
    inventory = parse_inventory({".github/workflows/ci.yml": WORKFLOW})

    assert inventory.ci_commands == (
        "pnpm run lint",
        "pnpm run test -- --coverage",
        "pnpm run build",
    )


def test_ci_parsing_survives_broken_yaml_and_ignores_other_files() -> None:
    assert (
        parse_inventory({".github/workflows/ci.yml": "\tnot: [yaml"}).ci_commands == ()
    )
    assert parse_inventory({"docs/ci.yml": WORKFLOW}).ci_commands == ()


def test_dependency_versions_are_read_from_both_manifests() -> None:
    files = {
        "package.json": json.dumps(
            {
                "dependencies": {"astro": "^4.5.0"},
                "devDependencies": {"vitest": "~1.2.0"},
            }
        ),
        "pyproject.toml": '[project]\ndependencies = ["fastapi>=0.115,<1.0", "httpx"]\n',
    }

    assert parse_inventory(files).dependencies == (
        ("astro", "^4.5.0"),
        ("vitest", "~1.2.0"),
        ("fastapi", ">=0.115,<1.0"),
        ("httpx", ""),
    )

# Headless turn runner and provider sandboxing

Planning and delegated execution need non-interactive model turns. A process
exit code alone does not prove that a turn used its tools successfully.

## Decision

Run each turn in a short-lived hardened container. The container has a
read-only root, no capabilities, no new privileges, bounded memory and process
counts, and only the required project-volume access.

Read-only planning turns mount the project volume read-only. Coding turns mount
the task branch writable and use write-capable provider flags.

The runner records tool outcomes, token use, duration, exit code, model, and
reported cost. A clean process exit with only failed tool calls is a failed
turn.

Claude is the implemented writable provider. Codex remains unsupported for
writable headless turns because its nested sandbox cannot start under the
container restrictions. Any future Codex path must treat the container as the
security boundary and verify correct tool execution before adoption.

## Consequences

The controller can measure failed and successful attempts. It can also keep
provider claims separate from git and verification evidence.

Model selection remains explicit and is retained with each run.

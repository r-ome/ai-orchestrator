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

Claude and Codex are implemented writable providers. Claude uses its
write-capable non-interactive mode. Codex uses `codex exec --json` with
`--sandbox danger-full-access` because its nested sandbox cannot start under
the container restrictions.

The Codex flag disables only the nested sandbox. The hardened container stays
the security boundary. Its read-only root, dropped capabilities,
no-new-privileges policy, resource limits, and task-scoped writable volume stay
in force. The controller reads Codex JSONL events and rejects a clean process
exit when every tool item failed.

## Consequences

The controller can measure failed and successful attempts. It can also keep
provider claims separate from git and verification evidence.

Model selection remains explicit and is retained with each run.

Codex reports token usage but no dollar amount in its JSONL completion event.
The controller stores that cost as unreported instead of recording zero.

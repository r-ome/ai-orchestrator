# Git substrate for task branches

A sandbox volume is populated from a source folder, including its `.git`
directory. The task layer needs a branch, a controller-verified commit, and a
fast-forward merge for each unit of work.

## Decision

Use the sandbox repository as the task substrate.

The controller creates one branch per task. It reads the branch and worktree
through a separate hardened container before it accepts a completion claim.
Acceptance uses a fast-forward-only merge. Rejection returns to the sandbox
branch and deletes only the task branch.

The controller supplies commit identity per command. It does not trust a
persistent git configuration inside the sandbox.

Sandbox registration removes copied remotes and custom hooks. Controller git
commands also bypass hooks. These controls reduce paths from a sandbox back to
the source repository.

## Consequences

Committed work remains recoverable until a person accepts or rejects it.
Uncommitted changes block settlement instead of being overwritten.

The task layer supports only repositories with a usable git baseline. A source
folder without git history needs a separate policy.

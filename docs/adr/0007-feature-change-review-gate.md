# Hold feature changes for whole-feature review

Feature change agents work against an implementation assembled from several accepted tasks. A successful build proves compilation, but it does not prove that a requested interaction behaves correctly.

## Decision

Pass the reviewed plan, implementation manifest, completed work, and earlier change requests into each feature change turn. Require observable acceptance criteria and evidence in the agent report.

Incorporate a verified change into the sandbox with `awaiting_review` status. Only an independent whole-feature review pinned to the current commit can mark held changes complete.

Record the sandbox's dirty worktree before the first delegated task starts. The
snapshot stores each Git status, path, file type, and SHA-256 content
fingerprint for regular files and symbolic links.

Before diff generation, review, and source merge, compare the current dirty
state with that immutable snapshot. Allow only exact matches. Report every new,
removed, status-changed, type-changed, or content-changed path.

Existing active sandboxes can have path-only task baselines. Upgrade one only
when every current dirty path remains under its earliest recorded baseline and
every recorded path still exists. Store the current fingerprints once. Do not
replace that snapshot during later checks.

## Consequences

People can preview and refine held changes without accepting each task. A stale or build-only review cannot authorize source delivery. Pre-existing user files remain in place when they stay unchanged. Any later uncommitted change blocks delivery with its path and change type. Each independent review adds one model turn; its runtime and cost remain unmeasured.

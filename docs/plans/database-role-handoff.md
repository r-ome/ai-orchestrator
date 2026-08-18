# Record — Candidate #4: the Database guest

**Repo:** `/Users/jeromeagapay/orchestrator_v2`
**Branch:** `refactor/database-role` — work commit `2a4472e`, rebased onto `main` @ `36cb8fc`
**Worktree:** `/Users/jeromeagapay/orchestrator_v2-db-role`
**Status:** done, not merged. 5 files, +70 / −157.

This started as a plan and is now a record. The review card for #4 proposed a
`DatabaseRole` module; verification showed the branches it would unify are
unreachable, so the work deleted them instead. Section 3 is the decision, section
5 is what was deliberately left in place.

---

## 1. What landed

`2a4472e refactor(previews): delete the unreachable database guest`

| File | Change |
|---|---|
| `backend/app/previews/service.py` | −86 lines: guest branches, `_share_candidates` |
| `backend/app/previews/models.py` | −13: `SharedDatabaseCandidate` and its two fields |
| `backend/tests/previews/test_shared_database.py` | −3 tests, +1 test |
| `frontend/src/api/previews.ts` | −12: the candidate types |
| `CONTEXT.md` | glossary entry marked historical |

**Verified:** `pytest` 789 passed / 43 skipped in `backend/`. `npx tsc -b` clean in
`frontend/`. Baseline on `main` @ `36cb8fc` is 791 / 43; the delta is −3
candidate-list tests and +1 new refusal test. Before the rebase the same delta read
783 against 785 at `08eec6d`.

---

## 2. The finding that drove it

`_validate_sharing` refuses `SHARED_DATA` outright. It landed in `98409a2`
(14 Aug 2026, *remove the legacy local-folder copy lifecycle*) and is pinned by
`tests/previews/test_shared_database.py`.

`_attach_shared_database` is the only writer of a `shared_database_schemas` row —
`record_shared_schema` has exactly one caller. So no new sandbox can become a
guest, and every row written from now on has `owner_sandbox_id == sandbox_id`.

The review card's premise was therefore stale. "A guest never owns that data" was
not an invariant four sites had to keep agreeing on. It was an invariant with no
live subject.

### Corrections to the review card

- Line numbers `717 · 1284 · 1310 · 1895 · 2062 · 2072 · 2109 · 2369` were all off
  by 3–15 lines.
- "8 branch sites" was 6. "10 branches become 4 reads" was 6 enum reads plus 5 row
  derivations, 3 of them unreachable.
- `models.py:90` was cited as a branch; it is a field default.
- The card was right that `owner` is recomputed from a row at teardown, and that
  `CONTEXT.md` defines **Database guest** with no module behind it.

---

## 3. What changed, and why each

### Deleted

**Guest suppression on the launch path.** `_start_native` now always runs the
approved migration and seed commands. The branch that skipped them for a guest
could not be reached.

**`share_target` resolution in `_attach_shared_database`.** The owner is now
always the sandbox itself, so `_shared_schema_name`, the collision check and
`record_shared_schema` all take `sandbox_id` directly.

**The whole share-candidates surface** — `_share_candidates`,
`SharedDatabaseCandidate`, `ProjectDatabaseSharing.candidates`,
`PreviewProposal.share_candidates`, and the matching frontend types. This was not
in the original plan. It surfaced during the work: the list exists to offer
schemas that another sandbox may join, and joining is refused, so the endpoint
offered a choice that could not be taken. No frontend component rendered it.

### Added

**A refusal at attach time.** `_validate_sharing` runs at *approval*
(`service.py:494`), not at start. An approval recorded before `98409a2` could
still reach the start path unguarded. `_attach_shared_database` now raises 422 as
its first statement, before any Docker work. The new test passes `None` for the
Docker client and settings to prove nothing is provisioned first.

One wording covers both raises, as `_SHARED_DATA_UNAVAILABLE` near the module
constants.

---

## 4. The one place the question still gets asked

`_release_shared_database` keeps its ownership check. The plan said it would
collapse to `drop_schema = ephemeral`. It should not: if a row written before
`98409a2` still exists, dropping the check makes a guest's teardown drop the
**owner's** schema. That is data loss to save about 10 lines.

It stays, and its docstring now says why — this is the one site that still asks
whether a row belongs to its sandbox. One site instead of four was the goal; the
module was only ever a means to it.

That is why the change is −87 net rather than the ~120 the plan estimated.

---

## 5. Left in place on purpose

- **`PreviewSharing.SHARED_DATA`** and `share_target`, with their validators.
  Rows and stored manifests written before `98409a2` must still parse. The enum
  docstring now says the value is historical and nothing can select it.
- **`describeSharing`'s `shared_data` branch** in
  `frontend/src/utils/databaseSharing.ts`. It renders the state of an existing
  row, so it is still reachable.
- **`app/controller/store.py`** — untouched. `record_shared_schema`,
  `shared_schema`, `shared_schemas_for_project` and `delete_shared_schema` all
  stand as they were.

---

## 6. Open, not addressed here

`fetchDatabaseSharing` in `frontend/src/api/previews.ts:251` has no caller. With
the candidate list gone, `GET /projects/{name}/database-sharing` returns one
sandbox's own coupling and nothing else, and no screen fetches it. Deleting the
endpoint is a separate decision about whether that view is wanted at all.

---

## 7. Merging

#3 merged to `main` while this was in progress, at `601f496`, `a2715fb` and
`36cb8fc`. This branch is rebased onto it with no conflict — #3 works inside
`app/sandboxes/`, this works inside `app/previews/`. It fast-forwards.

The work was done in a linked worktree at
`/Users/jeromeagapay/orchestrator_v2-db-role`, because the main checkout held #3's
uncommitted changes at the time. Remove it with `git worktree remove` once merged.

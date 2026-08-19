# Keep two shared-database implementations, separate on purpose

Two functions provision a project's shared database server:
`app/previews/sharing.py:133 _shared_database_server` and
`app/sandboxes/database.py:1393 _ensure_shared_server`. An architecture review read
this as duplication and scheduled a merge. It is not duplication. The two functions
serve two eras of the sandbox lifecycle, and one of them is MySQL-only by
construction.

`sandbox_database_runtime` (`app/sandboxes/database.py:1053`) alone decides which
path a preview takes. It returns `None`, sending the preview down the previews
path, when the sandbox has `lifecycle_version != "v1"` or `db_engine == "none"`.
Otherwise the sandbox owns a managed database and the sandboxes path serves it.

## Decision

Keep both implementations. Change no code.

Three behavioural differences remain between them:

| Difference | Effect |
|---|---|
| Image source: the proposal's `database.image` versus `SANDBOX_MYSQL_IMAGE` or `DEFAULT_MYSQL_IMAGE` | the two can disagree |
| Image-mismatch 409 fires before provisioning in sandboxes, after it in previews | previews pulls an image it then refuses |
| Health check raises `PreviewOperationError` versus `SandboxDatabaseError` | different status codes and wording reach the UI |

Two differences the review recorded as behavioural are not, and are written down
here so nobody re-derives them:

* **`database=` is inert.** Previews passes the proposal's database name and
  sandboxes passes `""`. `MySQLDatabaseEngine.provision` builds the container
  environment at `app/sandboxes/database.py:257` and reads `request.database` only
  when `request.shared` is false. Both call sites pass `shared=True`.
* **Container start is identical.** Previews starts a newly created container
  unconditionally; sandboxes starts it only when its status is not `running`.
  `create_hardened` returns the container unstarted
  (`app/containers/hardened.py:180`), so a new container has status `created` and
  both branches start it.

The `LABEL_DATA_MANAGED` difference on the shared volumes is also inert. Its only
reader, `_preview_volumes` (`app/previews/resources.py:134`), filters on that label
**and** a run id, and shared volumes deliberately carry no run id.

## Consequences

The shared container name keys on the project, not the sandbox
(`mysql_shared_database_names`, `app/sandboxes/database.py:917`). One project with
two v1 sandboxes, one with `db_engine = mysql` and one with `db_engine = none`,
therefore points both implementations at one container. Nothing refuses that
combination: `_validate_sharing` (`app/previews/sharing.py:562`) checks the preview
mode and the sharing kind, never the sandbox's own engine.

Only the image difference can harm that case. Whichever path creates the container
first records its image in a label, and the other gets a permanent 409 until an
operator deletes the container by hand. Detection currently proposes `mysql:8.4`
(`app/previews/detection.py:531`) and `DEFAULT_MYSQL_IMAGE` is `mysql:8.4`, so the
default case agrees.

The collision is latent, not live. Measured on 20 Aug 2026 against
`backend/.controller-data/controller.sqlite3` and its 18 Aug pre-reset backup:
one project, one sandbox (`v1`, `db_engine = none`), zero rows in
`sandbox_databases` and zero in `shared_database_schemas` in both stores. All four
recorded approvals declare `services: {}`. Neither implementation has ever
provisioned a database on this machine.

The names collide on MySQL only. Previews pins `MYSQL_DATABASE`
(`app/previews/sharing.py:61`) and waits on a MySQL health check, while
`shared_database_names` dispatches by engine and gives PostgreSQL a different
prefix. A PostgreSQL shared server is unreachable from the previews path.

**If the feature is ever used**, the fix is to give the image one authority — have
previews resolve it through `_database_image("mysql")`, or validate the proposal's
image against that value at approval. The fix is not to merge the two functions.
Threading every difference through one signature yields a twelve-parameter function
and leaves the shared container name, which is the part that actually collides,
exactly as it was.

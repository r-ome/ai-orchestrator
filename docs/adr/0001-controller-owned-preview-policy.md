# Keep preview policy in controller-owned SQLite

The controller records sandbox identity, approvals, lifecycle intent, and audit history in SQLite because editable sandboxes cannot be trusted to preserve policy. Docker and the filesystem remain authoritative for runtime and code facts, so startup reconciliation compares them with recorded intent instead of treating SQLite as a complete view of reality.

# Deleted and Moved Pages

A page that disappears from a database is never removed from SQLite; the next `--detect-deleted` run marks its row instead:

```bash
venv/bin/python sync.py --detect-deleted
```

This is a normal sync that also lists every page ID, so it is slower than the hourly run. Schedule it nightly as a separate task (same steps as [Scheduling](scheduling.md), with `sync.py --detect-deleted` as the argument), alongside the hourly one.

| `delete_state` | Meaning |
|---|---|
| `trashed` | In Notion's trash. Restoring it in Notion clears the mark on the next `--detect-deleted` run. |
| `gone` | Permanently deleted by Notion after its trash period, or the integration lost access (Notion's API cannot tell these apart). |
| `moved` | Still exists, but in another database. `moved_to` holds that database's config name if it is one you sync. |

Each row also has `deleted_at` (when it was first noticed). Live rows have all three columns empty, so filter current-state reports with `WHERE deleted_at IS NULL`. Every state change is also written to `{name}_changes` under the field `delete_state`.

If Notion returns far fewer pages than are stored (10 or more missing and under half remaining), nothing is marked and an error is logged, because that usually means lost access rather than real deletions. Change the threshold per database with `delete_guard_ratio` (default `0.5`; `0` turns the guard off).

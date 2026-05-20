# Notion PowerBI — Status
_Last updated: May 20, 2026_

## Sync Status

| Database | Configured | First Sync | Notes |
|---|---|---|---|
| tasks | Yes | [ ] | `include_content = true` — will be slow |
| recurring_task_definitions | Yes | [ ] | Rollup/relation cols excluded |
| areas | Yes | [ ] | |
| pursuits | Yes | [ ] | |
| shopping_list | Yes | [ ] | |

---

## Known Issues / Pre-Run Checklist

- [Done] Add `notion-api` pin to `requirements.txt`
- [ ] Run first sync — `venv/bin/python sync.py`
- [ ] Verify SQLite tables created and populated
- [ ] Confirm change tracking records initial state correctly
- [ ] Table name spaces: `"recurring task definitions"` and `"shopping list"` have spaces — valid in SQLite (quoted) but consider renaming to snake_case for cleaner Power BI table names
- [ ] README references `.env` / `config.yaml` — vestigial, needs cleanup
- [ ] No git repo yet — create on GitHub (standalone project, separate from Notion_Automator)

---

## Priorities

1. **First sync** — run `sync.py`, verify data in SQLite
2. **Power BI connection** — install SQLite ODBC driver on Windows, connect to `notion_powerbi.db`
3. **KPI dashboards** — Open/Closed volume, MTTR, due date drift, recurring task adherence (see PLANNED.md)
4. **Incremental sync** — filter by `last_edited_time`; `--full` flag already wired, just needs the logic
5. **README cleanup** — remove `.env`/`config.yaml` references
6. **Data Science / ML exploration** — after dashboard baseline is working (see PLANNED.md)

---

## Open Decisions

- **Table name spaces:** keep `"recurring task definitions"` / `"shopping list"` as-is, or rename to `recurring_task_definitions` / `shopping_list` before first sync (easier to rename now than after data exists)?
- **Last-sync timestamp storage:** sidecar JSON file vs `_meta` table in SQLite?
- **Power BI connection method:** SQLite ODBC driver vs CSV export?
- **Data Science exploration:** same project or separate `Data_Science` repo?

---

## Planned Features (summary)
- **Incremental sync** — filter by `last_edited_time` on subsequent runs → see PLANNED.md
- **KPI Dashboards** — Open/Closed volume, MTTR, adherence, drift, pursuit tracking → see PLANNED.md
- **Data Science / ML** — personal learning exploration using task history and/or Kaggle data → see PLANNED.md
- **README cleanup** → see PLANNED.md

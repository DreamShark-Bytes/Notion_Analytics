---
name: Bug Report
about: Something isn't working as expected
title: '[Bug] '
labels: bug
---

## What happened
<!-- What did the sync do that it shouldn't have, or fail to do that it should have? -->

## What I expected
<!-- What should have happened instead? -->

## Steps to reproduce
1. 
2. 
3. 

## Which area is affected
- [ ] Sync — pages not appearing or missing columns
- [ ] Change tracking — incorrect or missing change records
- [ ] Column rename migration
- [ ] CSV export
- [ ] SQLite schema / table structure
- [ ] Other: 

## Affected database
<!-- Which configured database name (e.g. "tasks", "pursuits")? -->

## Sync mode
- [ ] Normal run (`sync.py`)
- [ ] Full refresh (`sync.py --full`)

## Versions
<!-- Run: grep -r "__version__" venv/lib/*/site-packages/notion_api.py -->
- Notion PowerBI (date of last pull): 
- Notion API: 
- Python: 
- OS: 

## Relevant log output
<!-- Paste the relevant lines from notion_powerbi.log. Just the lines that matter. -->
```
paste log lines here
```

## Config snippet (if relevant)
<!-- Paste the affected [[databases]] block. Replace your token with ntn_... -->
```toml

```

## Related
<!-- Any related issues? -->

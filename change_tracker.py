"""
change_tracker.py
Compares a new page row against the stored snapshot and emits change records.

First-seen pages: all fields recorded as initial values with valid_from = page.created_time.
Subsequent syncs: only changed fields are recorded.

Fields excluded from tracking:
  - last_edited_time (changes every sync, not meaningful)
  - content_text (large, noisy)
  - url (never changes)

Users can further control tracking via change_fields / exclude_change_fields in config.
"""

from __future__ import annotations
from datetime import datetime, timezone

# Always skip these from change tracking regardless of config
_ALWAYS_SKIP = {"last_edited_time", "content_text", "url"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_changes(
    new_row: dict,
    prev_row: dict | None,
    change_fields: list[str],
    exclude_change_fields: list[str],
) -> list[dict]:
    """
    Compare new_row against prev_row and return a list of change records.

    Each record:
      {page_id, field, old_value, new_value, valid_from, detected_at}

    If prev_row is None (first sync for this page), all fields are recorded
    as initial values with old_value=None and valid_from=page.created_time.
    """
    detected_at = _now_iso()
    page_id = new_row["page_id"]
    created_time = new_row.get("created_time", detected_at)

    changes = []

    for field, new_val in new_row.items():
        if field in _ALWAYS_SKIP:
            continue
        if field in exclude_change_fields:
            continue
        if change_fields and field not in change_fields:
            continue

        if prev_row is None:
            # First time we see this page — record initial state
            changes.append({
                "page_id": page_id,
                "field": field,
                "old_value": None,
                "new_value": new_val,
                "valid_from": created_time,
                "detected_at": detected_at,
            })
        else:
            old_val = prev_row.get(field)
            # Compare as strings to handle type mismatches between SQLite and Python
            if _to_str(new_val) != _to_str(old_val):
                changes.append({
                    "page_id": page_id,
                    "field": field,
                    "old_value": old_val,
                    "new_value": new_val,
                    "valid_from": detected_at,
                    "detected_at": detected_at,
                })

    return changes


def _to_str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return "1" if val else "0"
    return str(val)

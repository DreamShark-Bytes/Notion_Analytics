import pytest
from change_tracker import detect_changes, _to_str


# ── _to_str ──────────────────────────────────────────────────────────────────

def test_to_str_none():
    assert _to_str(None) is None


def test_to_str_bool_true():
    assert _to_str(True) == "1"


def test_to_str_bool_false():
    assert _to_str(False) == "0"


def test_to_str_int():
    assert _to_str(42) == "42"


def test_to_str_str():
    assert _to_str("hello") == "hello"


def test_to_str_float():
    assert _to_str(3.14) == "3.14"


# ── detect_changes: first sync ────────────────────────────────────────────────

def _base_row(**kwargs):
    base = {
        "page_id": "p1",
        "created_time": "2026-01-01T00:00:00Z",
        "last_edited_time": "2026-01-01T00:00:00Z",
        "url": "https://notion.so/p1",
        "status": "Open",
    }
    base.update(kwargs)
    return base


def test_first_sync_records_non_skip_fields():
    row = _base_row()
    changes = detect_changes(row, None, [], [])
    fields = {c["field"] for c in changes}
    assert "status" in fields
    assert "page_id" in fields
    assert "created_time" in fields


def test_first_sync_skips_always_skip_fields():
    row = _base_row()
    changes = detect_changes(row, None, [], [])
    fields = {c["field"] for c in changes}
    assert "last_edited_time" not in fields
    assert "url" not in fields
    assert "content_text" not in fields


def test_first_sync_old_value_is_none():
    row = _base_row()
    changes = detect_changes(row, None, [], [])
    status_change = next(c for c in changes if c["field"] == "status")
    assert status_change["old_value"] is None
    assert status_change["new_value"] == "Open"


def test_first_sync_valid_from_is_created_time():
    row = _base_row()
    changes = detect_changes(row, None, [], [])
    for c in changes:
        assert c["valid_from"] == row["created_time"]


# ── detect_changes: subsequent syncs ─────────────────────────────────────────

def test_no_change_returns_empty():
    row = _base_row()
    changes = detect_changes(row, row.copy(), [], [])
    assert changes == []


def test_changed_field_detected():
    old = _base_row(status="Open")
    new = _base_row(status="Done")
    changes = detect_changes(new, old, [], [])
    status_change = next((c for c in changes if c["field"] == "status"), None)
    assert status_change is not None
    assert status_change["old_value"] == "Open"
    assert status_change["new_value"] == "Done"


def test_unchanged_field_not_emitted():
    old = _base_row(status="Open")
    new = _base_row(status="Open")
    changes = detect_changes(new, old, [], [])
    assert not any(c["field"] == "status" for c in changes)


def test_always_skip_not_tracked_even_when_changed():
    old = _base_row()
    old["last_edited_time"] = "2026-01-01"
    new = _base_row()
    new["last_edited_time"] = "2026-06-18"
    changes = detect_changes(new, old, [], [])
    assert not any(c["field"] == "last_edited_time" for c in changes)


# ── detect_changes: change_fields filter ─────────────────────────────────────

def test_change_fields_filter_limits_tracking():
    old = _base_row(status="Open")
    new = _base_row(status="Done")
    # only track "page_id" — "status" should not appear
    changes = detect_changes(new, old, ["page_id"], [])
    assert not any(c["field"] == "status" for c in changes)


def test_change_fields_empty_tracks_all():
    old = _base_row(status="Open")
    new = _base_row(status="Done")
    changes = detect_changes(new, old, [], [])
    assert any(c["field"] == "status" for c in changes)


def test_exclude_change_fields_skips_field():
    old = _base_row(status="Open")
    new = _base_row(status="Done")
    changes = detect_changes(new, old, [], ["status"])
    assert not any(c["field"] == "status" for c in changes)


# ── detect_changes: bool / type coercion ─────────────────────────────────────

def test_bool_true_equals_string_one():
    # SQLite stores "1" for True; Python receives True — should NOT fire a change
    old = {**_base_row(), "done": "1"}
    new = {**_base_row(), "done": True}
    changes = detect_changes(new, old, [], [])
    assert not any(c["field"] == "done" for c in changes)


def test_bool_false_equals_string_zero():
    old = {**_base_row(), "done": "0"}
    new = {**_base_row(), "done": False}
    changes = detect_changes(new, old, [], [])
    assert not any(c["field"] == "done" for c in changes)


def test_bool_true_not_equal_string_true():
    # Old TEXT "True" (pre-fix data) vs new bool True should fire a change
    old = {**_base_row(), "done": "True"}
    new = {**_base_row(), "done": True}
    changes = detect_changes(new, old, [], [])
    assert any(c["field"] == "done" for c in changes)


def test_none_to_value_is_change():
    old = {**_base_row(), "due_date_start": None}
    new = {**_base_row(), "due_date_start": "2026-06-01"}
    changes = detect_changes(new, old, [], [])
    assert any(c["field"] == "due_date_start" for c in changes)

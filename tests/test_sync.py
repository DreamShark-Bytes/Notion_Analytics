from sync import _expand_change_fields


def _row_with_dates():
    return {
        "page_id": "p1",
        "status": "Open",
        "due_date_start": "2026-06-01T09:00:00.000-05:00",
        "due_date_end": None,
        "closed_date_start": "2026-06-18T12:00:00.000-05:00",
        "closed_date_end": None,
    }


def test_base_field_in_row_unchanged():
    row = {"status": "Open", "due_date_start": "2026-06-01"}
    assert _expand_change_fields(["status"], row) == ["status"]


def test_split_field_expands_to_start_and_end():
    row = _row_with_dates()
    result = _expand_change_fields(["due_date"], row)
    assert result == ["due_date_start", "due_date_end"]


def test_split_field_only_start_exists():
    row = {"my_date_start": "2026-06-01"}
    result = _expand_change_fields(["my_date"], row)
    assert result == ["my_date_start"]


def test_unknown_field_preserved():
    # Field is in change_fields but not in this row — keep it (may appear on other pages)
    row = {"status": "Open"}
    result = _expand_change_fields(["nonexistent_field"], row)
    assert result == ["nonexistent_field"]


def test_empty_fields_returns_empty():
    assert _expand_change_fields([], _row_with_dates()) == []


def test_mixed_fields_expands_correctly():
    row = _row_with_dates()
    result = _expand_change_fields(["status", "due_date", "closed_date"], row)
    assert result == [
        "status",
        "due_date_start",
        "due_date_end",
        "closed_date_start",
        "closed_date_end",
    ]


def test_already_split_field_in_list_unchanged():
    # If someone puts "due_date_start" directly in change_fields, leave it alone
    row = _row_with_dates()
    result = _expand_change_fields(["due_date_start"], row)
    assert result == ["due_date_start"]


def test_expansion_is_idempotent():
    row = _row_with_dates()
    once = _expand_change_fields(["due_date"], row)
    twice = _expand_change_fields(once, row)
    assert once == twice


# ── Option ID wiring in record_change ─────────────────────────────────────────

def test_record_change_includes_option_ids():
    import sqlite3
    from storage import Storage

    s = Storage(":memory:")
    s.ensure_changes_table("tasks")

    s.record_change(
        "tasks", "p1", "status",
        "Open", "Done",
        "2026-06-18", "2026-06-18",
        old_value_id="opt_open",
        new_value_id="opt_done",
    )

    row = s.conn.execute(
        "SELECT old_value_id, new_value_id FROM tasks_changes WHERE field='status'"
    ).fetchone()
    assert row[0] == "opt_open"
    assert row[1] == "opt_done"


def test_record_change_null_ids_for_non_select():
    import sqlite3
    from storage import Storage

    s = Storage(":memory:")
    s.ensure_changes_table("tasks")

    s.record_change(
        "tasks", "p1", "name",
        "Old Name", "New Name",
        "2026-06-18", "2026-06-18",
    )

    row = s.conn.execute(
        "SELECT old_value_id, new_value_id FROM tasks_changes WHERE field='name'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None

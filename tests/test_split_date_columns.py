import sqlite3
import pytest
from split_date_columns import (
    _split_start,
    _split_end,
    auto_detect_date_columns,
    split_pages_table,
    split_changes_table,
)


# ── _split_start / _split_end ─────────────────────────────────────────────────

def test_split_start_plain_date():
    assert _split_start("2026-06-01") == "2026-06-01"


def test_split_start_plain_datetime():
    val = "2026-06-01T09:00:00.000-05:00"
    assert _split_start(val) == val


def test_split_start_range():
    assert _split_start("2026-06-01T03:00:00-05:00/2026-06-02T02:59:00-05:00") == "2026-06-01T03:00:00-05:00"


def test_split_start_none():
    assert _split_start(None) is None


def test_split_end_plain_date_returns_none():
    assert _split_end("2026-06-01") is None


def test_split_end_plain_datetime_returns_none():
    assert _split_end("2026-06-01T09:00:00.000-05:00") is None


def test_split_end_range():
    assert _split_end("2026-06-01T03:00:00-05:00/2026-06-02T02:59:00-05:00") == "2026-06-02T02:59:00-05:00"


def test_split_end_none():
    assert _split_end(None) is None


# ── auto_detect_date_columns ──────────────────────────────────────────────────

def _conn_with_date_col():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE test_pages (
            page_id TEXT PRIMARY KEY,
            due_date TEXT,
            name TEXT,
            count INTEGER
        )
    """)
    conn.execute("INSERT INTO test_pages VALUES ('p1', '2026-06-01T09:00:00.000-05:00', 'Task A', 1)")
    conn.execute("INSERT INTO test_pages VALUES ('p2', '2026-06-05T03:00:00.000-05:00/2026-06-06T02:59:00.000-05:00', 'Task B', 2)")
    return conn


def test_auto_detect_finds_date_column():
    cols = auto_detect_date_columns(_conn_with_date_col(), "test")
    assert "due_date" in cols


def test_auto_detect_ignores_non_date_text():
    cols = auto_detect_date_columns(_conn_with_date_col(), "test")
    assert "name" not in cols


def test_auto_detect_ignores_non_text_columns():
    cols = auto_detect_date_columns(_conn_with_date_col(), "test")
    assert "count" not in cols


def test_auto_detect_ignores_page_id():
    cols = auto_detect_date_columns(_conn_with_date_col(), "test")
    assert "page_id" not in cols


def test_auto_detect_ignores_already_split_columns():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE test_pages (
            page_id TEXT PRIMARY KEY,
            due_date_start TEXT,
            due_date_end TEXT
        )
    """)
    conn.execute("INSERT INTO test_pages VALUES ('p1', '2026-06-01', NULL)")
    cols = auto_detect_date_columns(conn, "test")
    assert "due_date_start" not in cols
    assert "due_date_end" not in cols


def test_auto_detect_missing_table_returns_empty():
    conn = sqlite3.connect(":memory:")
    cols = auto_detect_date_columns(conn, "nonexistent")
    assert cols == []


def test_auto_detect_all_null_returns_empty():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE test_pages (page_id TEXT PRIMARY KEY, due_date TEXT)")
    conn.execute("INSERT INTO test_pages VALUES ('p1', NULL)")
    cols = auto_detect_date_columns(conn, "test")
    assert "due_date" not in cols


# ── split_pages_table ─────────────────────────────────────────────────────────

def _pages_conn():
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute("""
        CREATE TABLE test_pages (
            page_id TEXT PRIMARY KEY,
            name TEXT,
            due_date TEXT
        )
    """)
    conn.execute("INSERT INTO test_pages VALUES ('p1', 'Task A', '2026-06-01T09:00:00.000-05:00')")
    conn.execute("INSERT INTO test_pages VALUES ('p2', 'Task B', '2026-06-05T03:00:00.000-05:00/2026-06-06T02:59:00.000-05:00')")
    conn.execute("INSERT INTO test_pages VALUES ('p3', 'Task C', NULL)")
    return conn


def _cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def test_split_pages_drops_original_column():
    conn = _pages_conn()
    split_pages_table(conn, "test", ["due_date"], dry_run=False)
    assert "due_date" not in _cols(conn, "test_pages")


def test_split_pages_adds_start_and_end():
    conn = _pages_conn()
    split_pages_table(conn, "test", ["due_date"], dry_run=False)
    cols = _cols(conn, "test_pages")
    assert "due_date_start" in cols
    assert "due_date_end" in cols


def test_split_pages_plain_date_value():
    conn = _pages_conn()
    split_pages_table(conn, "test", ["due_date"], dry_run=False)
    row = conn.execute("SELECT due_date_start, due_date_end FROM test_pages WHERE page_id='p1'").fetchone()
    assert row[0] == "2026-06-01T09:00:00.000-05:00"
    assert row[1] is None


def test_split_pages_range_date_value():
    conn = _pages_conn()
    split_pages_table(conn, "test", ["due_date"], dry_run=False)
    row = conn.execute("SELECT due_date_start, due_date_end FROM test_pages WHERE page_id='p2'").fetchone()
    assert row[0] == "2026-06-05T03:00:00.000-05:00"
    assert row[1] == "2026-06-06T02:59:00.000-05:00"


def test_split_pages_null_value():
    conn = _pages_conn()
    split_pages_table(conn, "test", ["due_date"], dry_run=False)
    row = conn.execute("SELECT due_date_start, due_date_end FROM test_pages WHERE page_id='p3'").fetchone()
    assert row[0] is None
    assert row[1] is None


def test_split_pages_dry_run_makes_no_changes():
    conn = _pages_conn()
    split_pages_table(conn, "test", ["due_date"], dry_run=True)
    assert "due_date" in _cols(conn, "test_pages")
    assert "due_date_start" not in _cols(conn, "test_pages")


def test_split_pages_missing_column_skipped():
    conn = _pages_conn()
    # "nonexistent" is not in the table — should not raise
    split_pages_table(conn, "test", ["nonexistent"], dry_run=False)
    assert _cols(conn, "test_pages") == {"page_id", "name", "due_date"}


# ── split_changes_table ───────────────────────────────────────────────────────

def _changes_conn():
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.execute("""
        CREATE TABLE test_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            valid_from TEXT,
            detected_at TEXT NOT NULL
        )
    """)
    # range → range change
    conn.execute("INSERT INTO test_changes (page_id, field, old_value, new_value, valid_from, detected_at) VALUES "
                 "('p1', 'due_date', '2026-06-01/2026-06-02', '2026-06-05/2026-06-06', '2026-06-05', '2026-06-05')")
    # plain → plain change
    conn.execute("INSERT INTO test_changes (page_id, field, old_value, new_value, valid_from, detected_at) VALUES "
                 "('p2', 'due_date', '2026-06-01', '2026-06-10', '2026-06-10', '2026-06-10')")
    # unrelated field — must not be touched
    conn.execute("INSERT INTO test_changes (page_id, field, old_value, new_value, valid_from, detected_at) VALUES "
                 "('p1', 'status', 'Open', 'Done', '2026-06-18', '2026-06-18')")
    return conn


def test_split_changes_removes_original_field_rows():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=False)
    count = conn.execute("SELECT COUNT(*) FROM test_changes WHERE field='due_date'").fetchone()[0]
    assert count == 0


def test_split_changes_creates_start_and_end_rows():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=False)
    fields = {r[0] for r in conn.execute("SELECT DISTINCT field FROM test_changes")}
    assert "due_date_start" in fields
    assert "due_date_end" in fields


def test_split_changes_range_start_values():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=False)
    row = conn.execute("SELECT old_value, new_value FROM test_changes WHERE field='due_date_start' AND page_id='p1'").fetchone()
    assert row[0] == "2026-06-01"
    assert row[1] == "2026-06-05"


def test_split_changes_range_end_values():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=False)
    row = conn.execute("SELECT old_value, new_value FROM test_changes WHERE field='due_date_end' AND page_id='p1'").fetchone()
    assert row[0] == "2026-06-02"
    assert row[1] == "2026-06-06"


def test_split_changes_plain_date_end_is_null():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=False)
    row = conn.execute("SELECT old_value, new_value FROM test_changes WHERE field='due_date_end' AND page_id='p2'").fetchone()
    assert row[0] is None
    assert row[1] is None


def test_split_changes_preserves_unrelated_field():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=False)
    count = conn.execute("SELECT COUNT(*) FROM test_changes WHERE field='status'").fetchone()[0]
    assert count == 1


def test_split_changes_dry_run_makes_no_changes():
    conn = _changes_conn()
    split_changes_table(conn, "test", ["due_date"], dry_run=True)
    count = conn.execute("SELECT COUNT(*) FROM test_changes WHERE field='due_date'").fetchone()[0]
    assert count == 2

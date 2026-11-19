"""Renames, name collisions and deleted properties: one property keeps one continuous history."""
import pytest

from extractor import extract_page_row
from storage import Storage, physical_columns
from sync import _compare_schema, sync_database

CFG = {"database_id": "d1", "name": "tasks", "include_content": False, "include_comments": False}


def _storage() -> Storage:
    s = Storage(":memory:")
    s.ensure_schema_table()
    s.ensure_options_table()
    s.ensure_backups_table()
    s.ensure_schema_changes_table()
    return s


def _schema(*props) -> dict:
    return {"properties": {name: {"id": pid, "type": t, t: {}} for pid, name, t in props}}


def _cols(s, table="tasks"):
    return [r[1] for r in s.conn.execute(f'PRAGMA table_info("{table}_pages")')]


def _fields(s):
    return {r[0] for r in s.conn.execute('SELECT DISTINCT field FROM "tasks_changes"')}


def _kinds(s):
    return [r[0] for r in s.conn.execute('SELECT kind FROM "_schema_changes" ORDER BY id')]


class FakeClient:
    def __init__(self, props, pages):
        self.props, self.pages = props, pages

    def get_database(self, _):
        return {"properties": {n: {"id": i, "type": t, t: {"options": []} if t in ("select", "status") else {}}
                               for i, n, t in self.props}}

    def query_database(self, _, filter_payload=None):
        return self.pages


def _page(edited, **props):
    return {"id": "p1", "created_time": "2026-10-01T00:00:00.000Z", "last_edited_time": edited, "url": "u",
            "properties": props}


def _sync(s, props, page):
    assert sync_database(FakeClient(props, [page]), CFG, s, full=True) is True


PROPS_V1 = [("t", "Name", "title"), ("sel", "Effort Level", "select"), ("dt", "Due Date", "date")]
PROPS_V2 = [("t", "Name", "title"), ("sel", "Effort", "select"), ("dt", "Deadline", "date")]


def _page_v1(edited, effort, due):
    return _page(edited, **{"Effort Level": {"select": {"id": "s1", "name": effort}},
                            "Due Date": {"date": {"start": due, "end": None}}})


def _page_v2(edited, effort, due):
    return _page(edited, **{"Effort": {"select": {"id": "s1", "name": effort}},
                            "Deadline": {"date": {"start": due, "end": None}}})


def _seeded():
    s = Storage(":memory:")
    _sync(s, PROPS_V1, _page_v1("2026-11-01T00:00:00.000Z", "high", "2026-11-05"))
    _sync(s, PROPS_V1, _page_v1("2026-11-02T00:00:00.000Z", "low", "2026-11-06"))
    return s


# ── a rename moves every physical column and the history ──────────────────────

def test_physical_columns_match_what_the_extractor_writes():
    cases = {"date": {"date": {"start": "2026-01-01", "end": None}},
             "select": {"select": {"id": "x", "name": "n"}},
             "status": {"status": {"id": "x", "name": "n"}},
             "multi_select": {"multi_select": [{"id": "x", "name": "n"}]},
             "number": {"number": 1}}
    for prop_type, value in cases.items():
        schema = {"properties": {"Thing": {"id": "i", "type": prop_type}}}
        row = extract_page_row({"id": "p", "properties": {"Thing": value}}, schema, [], [])
        written = set(row) - {"page_id", "created_time", "last_edited_time", "url"}
        assert written == set(physical_columns("thing", prop_type)), prop_type


def test_rename_moves_date_and_select_id_columns_and_history():
    s = _seeded()
    _sync(s, PROPS_V2, _page_v2("2026-11-03T00:00:00.000Z", "mid", "2026-11-07"))
    cols = _cols(s)
    assert {"effort", "effort_id", "deadline_start", "deadline_end"} <= set(cols)
    assert not {"effort_level", "effort_level_id", "due_date_start", "due_date_end"} & set(cols)
    assert {"effort", "deadline_start"} <= _fields(s)
    assert not {"effort_level", "due_date_start", "due_date_end"} & _fields(s)
    history = [(r[0], r[1]) for r in s.conn.execute(
        'SELECT old_value, new_value FROM "tasks_changes" WHERE field = "deadline_start" ORDER BY id')]
    assert history == [(None, "2026-11-05"), ("2026-11-05", "2026-11-06"), ("2026-11-06", "2026-11-07")]
    assert "property_renamed" in _kinds(s)


def test_rename_is_all_or_nothing(monkeypatch):
    s = _seeded()
    before_cols, before_fields = _cols(s), _fields(s)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(s, "_insert_schema_change", boom)
    with pytest.raises(RuntimeError):
        _compare_schema("tasks", _schema(*PROPS_V2), s, "t1")
    assert _cols(s) == before_cols and _fields(s) == before_fields
    assert s.get_stored_schema("tasks")["sel"]["column_name"] == "effort_level"


def test_retry_after_the_columns_were_already_renamed_fixes_the_history():
    s = _seeded()
    s.conn.execute('ALTER TABLE "tasks_pages" RENAME COLUMN "effort_level" TO "effort"')
    s.conn.commit()  # a run that stopped right after the first step of the old code
    _compare_schema("tasks", _schema(*PROPS_V2), s, "t1")
    assert "effort" in _fields(s) and "effort_level" not in _fields(s)


def test_a_case_only_rename_changes_no_column():
    s = _seeded()
    _compare_schema("tasks", _schema(("t", "Name", "title"), ("sel", "effort level", "select"),
                                     ("dt", "Due Date", "date")), s, "t1")
    assert "effort_level" in _cols(s)
    assert s.get_stored_schema("tasks")["sel"]["property_name"] == "effort level"


# ── name collisions ───────────────────────────────────────────────────────────

def test_new_property_with_a_taken_name_gets_a_suffix():
    s = _seeded()
    col_map = {}
    _compare_schema("tasks", _schema(*PROPS_V1, ("n", "Effort-Level", "rich_text")), s, "t1", col_map=col_map)
    assert col_map["Effort-Level"] == "effort_level_2"
    assert s.get_stored_schema("tasks")["n"]["column_name"] == "effort_level_2"


def test_a_name_that_only_collides_through_a_derived_column_is_caught():
    s = _storage()
    col_map = {}
    # a date "Due" writes due_start and due_end; a text property "Due Start" would write due_start as well
    _compare_schema("tasks", _schema(("a", "Due", "date"), ("b", "Due Start", "rich_text")), s, "t1", col_map=col_map)
    assert col_map["Due"] == "due"
    assert col_map["Due Start"] == "due_start_2"


def test_a_property_named_like_a_system_column_gets_a_suffix():
    s = _storage()
    col_map = {}
    _compare_schema("tasks", _schema(("a", "URL", "url")), s, "t1", col_map=col_map)
    assert col_map["URL"] == "url_2"


def test_a_generated_suffix_is_not_reused():
    s = _storage()
    col_map = {}
    _compare_schema("tasks", _schema(("a", "Done", "checkbox"), ("b", "Done ", "checkbox"),
                                     ("c", "Done 2", "checkbox")), s, "t1", col_map=col_map)
    assert sorted(col_map.values()) == ["done", "done_2", "done_2_2"]


def test_rename_onto_a_taken_name_uses_a_suffix_and_does_not_crash():
    s = _storage()
    _compare_schema("tasks", _schema(("a", "Effort", "select"), ("b", "Other", "rich_text")), s, "t0")
    s.ensure_pages_table("tasks", {"effort": "x", "effort_id": "x", "other": "x"})
    # "Effort" is renamed to "Other" while the other property is renamed to "Other-" (the same column name)
    _compare_schema("tasks", _schema(("a", "Other", "select"), ("b", "Other-", "rich_text")), s, "t1")
    stored = s.get_stored_schema("tasks")
    assert stored["b"]["column_name"] == "other"
    assert stored["a"]["column_name"] == "other_2"
    assert {"other_2", "other_2_id"} <= set(_cols(s))


# ── a deleted property and a new one with the same name ───────────────────────

def test_deleted_property_column_is_renamed_when_a_new_property_needs_the_name():
    s = _storage()
    _compare_schema("tasks", _schema(("a", "Other", "rich_text")), s, "2026-11-01T00:00:00Z")
    s.ensure_pages_table("tasks", {"other": "x"})
    s.conn.execute('INSERT INTO "tasks_pages" (page_id, other) VALUES ("p1", "old value")')
    s.conn.execute('CREATE TABLE "tasks_changes" (id INTEGER PRIMARY KEY, page_id TEXT, field TEXT, old_value TEXT, '
                   'new_value TEXT, old_value_id TEXT, new_value_id TEXT, valid_from TEXT, detected_at TEXT)')
    s.conn.execute('INSERT INTO "tasks_changes" (page_id, field, detected_at) VALUES ("p1", "other", "t")')
    s.conn.commit()
    col_map = {}
    _compare_schema("tasks", _schema(("b", "Other", "rich_text")), s, "2026-11-19T00:00:00Z", col_map=col_map)
    schema = s.get_stored_schema("tasks")
    assert col_map["Other"] == "other"
    assert schema["b"]["column_name"] == "other"
    assert schema["a"]["column_name"] == "other_del_20261119"
    s.ensure_pages_table("tasks", {"other": "x"})  # the sync adds the new property's column when it saves a page
    row = s.conn.execute("SELECT other, other_del_20261119 FROM tasks_pages").fetchone()
    assert (row[0], row[1]) == (None, "old value")
    assert _fields(s) == {"other_del_20261119"}
    assert {"property_removed", "orphan_column_renamed", "property_added"} <= set(_kinds(s))


def test_deleted_property_keeps_its_column_when_nothing_needs_the_name():
    s = _storage()
    _compare_schema("tasks", _schema(("a", "Other", "rich_text"), ("k", "Keep", "rich_text")), s, "t0")
    s.ensure_pages_table("tasks", {"other": "x", "keep": "x"})
    _compare_schema("tasks", _schema(("k", "Keep", "rich_text")), s, "t1")
    _compare_schema("tasks", _schema(("k", "Keep", "rich_text")), s, "t2")
    assert "other" in _cols(s)
    assert _kinds(s).count("property_removed") == 1  # logged once, not every run


def test_deleted_date_property_retires_both_date_columns():
    s = _storage()
    _compare_schema("tasks", _schema(("a", "Due", "date")), s, "2026-11-01T00:00:00Z")
    s.ensure_pages_table("tasks", {"due_start": "x", "due_end": "x"})
    _compare_schema("tasks", _schema(("b", "Due", "rich_text")), s, "2026-11-19T00:00:00Z")
    cols = _cols(s)
    assert {"due_del_20261119_start", "due_del_20261119_end"} <= set(cols)
    assert s.get_stored_schema("tasks")["b"]["column_name"] == "due"


def test_the_unique_index_stops_two_properties_sharing_a_column():
    s = _storage()
    s.upsert_schema("tasks", "a", "One", "same", "rich_text", "t")
    with pytest.raises(Exception):
        s.upsert_schema("tasks", "b", "Two", "same", "rich_text", "t")

import pytest
import requests

from storage import Storage
from sync import (
    _classify_missing,
    _find_configured_table,
    _parent_source_id,
    _reconcile_deleted,
    sync_database,
)

DB_ID = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_DB_ID = "bbbbbbbb-0000-0000-0000-000000000002"
TASKS = {"database_id": DB_ID, "name": "tasks", "include_content": False, "include_comments": False}
ARCHIVE = {"database_id": OTHER_DB_ID, "name": "archive", "include_content": False, "include_comments": False}
SCHEMA = {"properties": {"Status": {"id": "st", "type": "select", "select": {"options": []}}}}


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


def _page(pid: str, edited: str = "2026-11-01T00:00:00.000Z", status: str = "Open") -> dict:
    return {
        "id": pid,
        "created_time": "2026-10-01T00:00:00.000Z",
        "last_edited_time": edited,
        "url": f"https://notion.so/{pid}",
        "properties": {"Status": {"select": {"id": "s1", "name": status}}},
    }


class FakeClient:
    """query_database returns `pages`; get_page returns/raises per page_id from `lookup`."""

    def __init__(self, pages=(), lookup=None):
        self.pages = list(pages)
        self.lookup = lookup or {}
        self.get_page_calls: list[str] = []
        self.query_filters: list = []

    def get_database(self, db_id):
        return SCHEMA

    def query_database(self, db_id, filter_payload=None):
        self.query_filters.append(filter_payload)
        return list(self.pages)

    def get_page(self, page_id):
        self.get_page_calls.append(page_id)
        result = self.lookup[page_id]
        if isinstance(result, Exception):
            raise result
        return result


def _trashed_page(pid):
    return {"id": pid, "archived": True, "parent": {"type": "database_id", "database_id": DB_ID}}


def _moved_page(pid, parent_db):
    return {"id": pid, "archived": False, "parent": {"type": "database_id", "database_id": parent_db}}


@pytest.fixture
def storage():
    s = Storage(":memory:")
    yield s
    s.close()


def _seed(storage, ids):
    """First full sync of `ids` into tasks."""
    sync_database(FakeClient([_page(i) for i in ids]), TASKS, storage, full=True)


def _row(storage, pid):
    return storage.get_page("tasks", pid)


def _changes(storage, pid, field="delete_state"):
    cur = storage.conn.execute(
        'SELECT old_value, new_value FROM "tasks_changes" WHERE page_id = ? AND field = ? ORDER BY id',
        (pid, field),
    )
    return [(r["old_value"], r["new_value"]) for r in cur.fetchall()]


# ── helpers ───────────────────────────────────────────────────────────────────

def test_parent_source_id_reads_database_or_data_source():
    assert _parent_source_id({"parent": {"database_id": "d1"}}) == "d1"
    assert _parent_source_id({"parent": {"data_source_id": "ds1", "database_id": "d1"}}) == "ds1"
    assert _parent_source_id({"parent": {"page_id": "p1"}}) is None
    assert _parent_source_id({}) is None


def test_find_configured_table_ignores_hyphens_and_case():
    page = {"parent": {"database_id": OTHER_DB_ID.replace("-", "").upper()}}
    assert _find_configured_table(page, [TASKS, ARCHIVE]) == "archive"


def test_find_configured_table_unconfigured_parent_is_none():
    page = {"parent": {"database_id": "cccccccc-0000-0000-0000-000000000003"}}
    assert _find_configured_table(page, [TASKS, ARCHIVE]) is None


# ── classification ────────────────────────────────────────────────────────────

def test_classify_trashed_via_archived_flag():
    client = FakeClient(lookup={"p1": _trashed_page("p1")})
    assert _classify_missing(client, "p1", [TASKS]) == ("trashed", None)


def test_classify_trashed_via_in_trash_flag():
    client = FakeClient(lookup={"p1": {"id": "p1", "in_trash": True, "parent": {}}})
    assert _classify_missing(client, "p1", [TASKS]) == ("trashed", None)


def test_classify_gone_on_404():
    client = FakeClient(lookup={"p1": _http_error(404)})
    assert _classify_missing(client, "p1", [TASKS]) == ("gone", None)


def test_classify_moved_to_configured_table():
    client = FakeClient(lookup={"p1": _moved_page("p1", OTHER_DB_ID)})
    assert _classify_missing(client, "p1", [TASKS, ARCHIVE]) == ("moved", "archive")


def test_classify_moved_to_unconfigured_parent():
    client = FakeClient(lookup={"p1": _moved_page("p1", "cccccccc-0000-0000-0000-000000000003")})
    assert _classify_missing(client, "p1", [TASKS, ARCHIVE]) == ("moved", None)


@pytest.mark.parametrize("status", [429, 500, 403])
def test_classify_transient_error_is_undecided(status):
    client = FakeClient(lookup={"p1": _http_error(status)})
    assert _classify_missing(client, "p1", [TASKS]) is None


# ── reconcile ─────────────────────────────────────────────────────────────────

def test_missing_page_marked_trashed_with_change_row(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient(lookup={"p2": _trashed_page("p2")})
    result = _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "2026-11-06T00:00:00+00:00", True)

    assert result["marked"] == 1
    row = _row(storage, "p2")
    assert (row["deleted_at"], row["delete_state"], row["moved_to"]) == ("2026-11-06T00:00:00+00:00", "trashed", None)
    assert _row(storage, "p1")["deleted_at"] is None
    assert _changes(storage, "p2") == [(None, "trashed")]
    assert client.get_page_calls == ["p2"]


def test_trashed_page_becomes_gone_and_keeps_first_deleted_at(storage):
    _seed(storage, ["p1", "p2"])
    first = FakeClient(lookup={"p2": _trashed_page("p2")})
    _reconcile_deleted(first, TASKS, storage, {"p1"}, [TASKS], "2026-11-06T00:00:00+00:00", True)

    second = FakeClient(lookup={"p2": _http_error(404)})
    result = _reconcile_deleted(second, TASKS, storage, {"p1"}, [TASKS], "2026-12-07T00:00:00+00:00", True)

    assert result["updated"] == 1
    row = _row(storage, "p2")
    assert row["delete_state"] == "gone"
    assert row["deleted_at"] == "2026-11-06T00:00:00+00:00"
    assert _changes(storage, "p2") == [(None, "trashed"), ("trashed", "gone")]


def test_gone_page_is_not_rechecked(storage):
    _seed(storage, ["p1", "p2"])
    _reconcile_deleted(FakeClient(lookup={"p2": _http_error(404)}), TASKS, storage, {"p1"}, [TASKS], "t1", True)

    again = FakeClient()
    _reconcile_deleted(again, TASKS, storage, {"p1"}, [TASKS], "t2", True)
    assert again.get_page_calls == []


def test_unchanged_trashed_page_records_no_new_change(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient(lookup={"p2": _trashed_page("p2")})
    _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t1", True)
    _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t2", True)
    assert _changes(storage, "p2") == [(None, "trashed")]


def test_moved_page_records_destination(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient(lookup={"p2": _moved_page("p2", OTHER_DB_ID)})
    _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS, ARCHIVE], "t1", True)
    row = _row(storage, "p2")
    assert (row["delete_state"], row["moved_to"]) == ("moved", "archive")


def test_page_still_in_this_database_is_not_marked(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient(lookup={"p2": _moved_page("p2", DB_ID)})
    result = _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t1", True)
    assert result["skipped"] == 1
    assert _row(storage, "p2")["deleted_at"] is None


def test_transient_error_leaves_page_live_for_retry(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient(lookup={"p2": _http_error(500)})
    result = _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t1", True)
    assert result["skipped"] == 1
    assert _row(storage, "p2")["deleted_at"] is None


def test_no_change_rows_when_tracking_off(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient(lookup={"p2": _trashed_page("p2")})
    _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t1", False)
    assert _row(storage, "p2")["delete_state"] == "trashed"
    assert _changes(storage, "p2") == []


# ── safety guard ──────────────────────────────────────────────────────────────

def test_guard_aborts_when_notion_returns_nothing(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient()
    result = _reconcile_deleted(client, TASKS, storage, set(), [TASKS], "t1", True)
    assert result["aborted"] is True
    assert client.get_page_calls == []
    assert storage.get_live_page_ids("tasks") == {"p1", "p2"}


def test_guard_aborts_on_mass_disappearance(storage):
    ids = [f"p{i}" for i in range(20)]
    _seed(storage, ids)
    client = FakeClient()
    result = _reconcile_deleted(client, TASKS, storage, set(ids[:5]), [TASKS], "t1", True)
    assert result["aborted"] is True
    assert len(storage.get_live_page_ids("tasks")) == 20


def test_guard_does_not_block_a_few_deletions_in_a_small_database(storage):
    _seed(storage, ["p1", "p2", "p3", "p4"])
    client = FakeClient(lookup={i: _trashed_page(i) for i in ("p2", "p3", "p4")})
    result = _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t1", True)
    assert result["aborted"] is False
    assert result["marked"] == 3


def test_guard_ratio_zero_disables_the_guard(storage):
    ids = [f"p{i}" for i in range(20)]
    _seed(storage, ids)
    cfg = {**TASKS, "delete_guard_ratio": 0}
    client = FakeClient(lookup={i: _trashed_page(i) for i in ids[5:]})
    result = _reconcile_deleted(client, cfg, storage, set(ids[:5]), [cfg], "t1", True)
    assert result["aborted"] is False
    assert result["marked"] == 15


# ── end to end through sync_database ──────────────────────────────────────────

def test_default_sync_never_runs_detection(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient([_page("p1")], lookup={"p2": _trashed_page("p2")})
    sync_database(client, TASKS, storage, full=False)
    assert client.get_page_calls == []
    assert _row(storage, "p2")["deleted_at"] is None


def test_detect_deleted_marks_missing_page(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient([_page("p1")], lookup={"p2": _trashed_page("p2")})
    sync_database(client, TASKS, storage, full=False, detect_deleted=True, all_db_cfgs=[TASKS])
    assert client.query_filters == [None]  # detection needs the unfiltered ID list
    assert _row(storage, "p2")["delete_state"] == "trashed"
    assert _row(storage, "p1")["deleted_at"] is None


def test_restored_page_clears_delete_columns_and_records_change(storage):
    _seed(storage, ["p1", "p2"])
    gone = FakeClient([_page("p1")], lookup={"p2": _trashed_page("p2")})
    sync_database(gone, TASKS, storage, full=False, detect_deleted=True, all_db_cfgs=[TASKS])

    # p2 returns with an OLD last_edited_time, as if restore did not bump it
    back = FakeClient([_page("p1"), _page("p2", edited="2020-01-01T00:00:00.000Z")])
    sync_database(back, TASKS, storage, full=False, detect_deleted=True, all_db_cfgs=[TASKS])

    row = _row(storage, "p2")
    assert (row["deleted_at"], row["delete_state"], row["moved_to"]) == (None, None, None)
    assert _changes(storage, "p2") == [(None, "trashed"), ("trashed", None)]


def test_new_page_with_old_edit_time_is_fetched_during_detection(storage):
    """A page moved in from another database may keep an old last_edited_time."""
    _seed(storage, ["p1"])
    client = FakeClient([_page("p1"), _page("p9", edited="2020-01-01T00:00:00.000Z")])
    sync_database(client, TASKS, storage, full=False, detect_deleted=True, all_db_cfgs=[TASKS])
    assert _row(storage, "p9") is not None


def test_unchanged_old_pages_are_not_refetched_during_detection(storage):
    _seed(storage, ["p1", "p2"])
    client = FakeClient([_page("p1", edited="2020-01-01T00:00:00.000Z"), _page("p2", edited="2020-01-01T00:00:00.000Z")])
    sync_database(client, TASKS, storage, full=False, detect_deleted=True, all_db_cfgs=[TASKS])
    # Neither page was upserted again, so the stored edit time is still the seed value
    assert _row(storage, "p1")["last_edited_time"] == "2026-11-01T00:00:00.000Z"


def test_existing_table_gains_delete_columns(storage):
    """A _pages table created before this feature is migrated in place, data intact."""
    storage.conn.execute(
        'CREATE TABLE "tasks_pages" (page_id TEXT PRIMARY KEY, created_time TEXT, last_edited_time TEXT, url TEXT, content_text TEXT)'
    )
    storage.conn.execute('INSERT INTO "tasks_pages" (page_id) VALUES (\'old1\')')
    storage.conn.commit()
    assert storage.ensure_deletion_columns("tasks") is True
    row = _row(storage, "old1")
    assert row["deleted_at"] is None and row["delete_state"] is None and row["moved_to"] is None
    assert storage.get_live_page_ids("tasks") == {"old1"}


def test_ensure_deletion_columns_false_when_table_missing(storage):
    assert storage.ensure_deletion_columns("nope") is False


# ── log volume ────────────────────────────────────────────────────────────────

def test_many_marked_pages_log_ten_lines_and_one_summary(storage, caplog):
    ids = [f"p{i:02d}" for i in range(25)]
    _seed(storage, ids)
    cfg = {**TASKS, "delete_guard_ratio": 0}
    client = FakeClient(lookup={i: _trashed_page(i) for i in ids[1:]})
    with caplog.at_level("INFO", logger="sync"):
        result = _reconcile_deleted(client, cfg, storage, {"p00"}, [cfg], "t1", True)
    assert result["marked"] == 24
    page_lines = [m for m in caplog.messages if "Page p" in m]
    assert len(page_lines) == 10
    summary = [m for m in caplog.messages if "more page(s) changed delete state" in m]
    assert len(summary) == 1 and "14 more" in summary[0]


def test_few_marked_pages_log_each_and_no_summary(storage, caplog):
    _seed(storage, ["p1", "p2", "p3"])
    client = FakeClient(lookup={"p2": _trashed_page("p2"), "p3": _trashed_page("p3")})
    with caplog.at_level("INFO", logger="sync"):
        _reconcile_deleted(client, TASKS, storage, {"p1"}, [TASKS], "t1", True)
    assert len([m for m in caplog.messages if "Page p" in m]) == 2
    assert not any("more page(s)" in m for m in caplog.messages)

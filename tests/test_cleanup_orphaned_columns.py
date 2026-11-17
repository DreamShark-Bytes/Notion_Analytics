import pytest
from cleanup_orphaned_columns import find_orphans
from storage import Storage
from sync import sync_database
from test_deleted_pages import TASKS, FakeClient, _page


@pytest.fixture
def storage_with_soft_delete_columns():
    """A database as the laptop has it after `sync.py --detect-deleted` has run."""
    s = Storage(":memory:")
    sync_database(FakeClient([_page("p1")]), TASKS, s, full=True)
    assert s.ensure_deletion_columns("tasks") is True
    yield s
    s.close()


def test_soft_delete_columns_are_not_reported_as_orphans(storage_with_soft_delete_columns):
    assert find_orphans(storage_with_soft_delete_columns.conn, "tasks") == []


def test_a_real_orphan_is_still_reported_next_to_the_soft_delete_columns(storage_with_soft_delete_columns):
    conn = storage_with_soft_delete_columns.conn
    conn.execute('ALTER TABLE "tasks_pages" ADD COLUMN "legacy_col" TEXT')
    assert "legacy_col" in find_orphans(conn, "tasks")

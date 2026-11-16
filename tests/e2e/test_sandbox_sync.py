"""
End-to-end tests against a real Notion sandbox page.

Opt-in: set NOTION_E2E_CONFIG to a TOML file whose `token` can see the sandbox page
(the file is only read for `token`, so a copy of config.toml works). Without it every
test here is skipped. The sandbox page id comes from NOTION_E2E_PAGE or the untracked file
tests/e2e/sandbox_page.txt (never commit it). The tests create one database under the sandbox page, change it
step by step, run `sync.py` as a subprocess after each step, and assert on the SQLite
file. Writes go only to that database; it is trashed at the end.

    NOTION_E2E_CONFIG=config_sandbox.toml python -m pytest tests/e2e -v

The tests depend on each other and run in file order. They need the real `notion_api`
(>= 1.3.0, for patch_database), so the stub installed by the root conftest is bypassed.
"""
import importlib.machinery
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from extractor import sanitize_col

ROOT = Path(__file__).resolve().parents[2]
_PAGE_FILE = Path(__file__).with_name("sandbox_page.txt")  # untracked: holds the sandbox page id
SANDBOX_PAGE = os.environ.get("NOTION_E2E_PAGE") or (_PAGE_FILE.read_text().strip() if _PAGE_FILE.exists() else "")
CONFIG = os.environ.get("NOTION_E2E_CONFIG")
TABLE = "e2e"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not CONFIG, reason="set NOTION_E2E_CONFIG to run the sandbox tests"),
    pytest.mark.skipif(not SANDBOX_PAGE, reason="set NOTION_E2E_PAGE or put the sandbox page id in tests/e2e/sandbox_page.txt"),
]


def _real_client(token):
    """Load the installed notion_api even though conftest.py put a stub in sys.modules."""
    spec = importlib.machinery.PathFinder.find_spec("notion_api")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NotionClient(token=token)


def _title(text):
    return {"title": [{"text": {"content": text}}]}


def _rich(text):
    return {"rich_text": [{"text": {"content": text}}]}


class Env:
    def __init__(self, token, tmp):
        self.client = _real_client(token)
        self.tmp = Path(tmp)
        self.db_path = self.tmp / "e2e.db"
        self.config_path = self.tmp / "e2e.toml"
        self.db_id = None
        self.page_ids = {}

    def write_config(self):
        self.config_path.write_text(
            f'token = "{self.client.token}"\n'
            f"[[databases]]\nname = \"{TABLE}\"\ndatabase_id = \"{self.db_id}\"\n"
            f'[output]\ndb_path = "{self.db_path}"\n'
        )

    def sync(self, *flags):
        # cwd = tmp so the log file lands there, not in the project
        return subprocess.run(
            [sys.executable, str(ROOT / "sync.py"), "--config", str(self.config_path), *flags],
            cwd=self.tmp, capture_output=True, text=True, timeout=600,
        )

    def query(self, sql, *args):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def columns(self):
        return {r["name"] for r in self.query(f'PRAGMA table_info("{TABLE}_pages")')}

    def row(self, name):
        return self.query(f'SELECT * FROM "{TABLE}_pages" WHERE "name" = ?', name)[0]


@pytest.fixture(scope="module")
def env():
    token = tomllib.loads(Path(CONFIG).read_text())["token"]
    with tempfile.TemporaryDirectory() as tmp:
        e = Env(token, tmp)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        db = e.client.create_database(
            SANDBOX_PAGE, f"e2e_{stamp} (created and trashed by Notion_Analytics tests)",
            {
                "Name": {"title": {}},
                "Text": {"rich_text": {}},
                "Num": {"number": {}},
                "Pick": {"select": {"options": [{"name": "A"}, {"name": "B"}]}},
                "Tags": {"multi_select": {"options": [{"name": "x"}, {"name": "y"}]}},
                "When": {"date": {}},
                "Done": {"checkbox": {}},
                "Link": {"url": {}},
            },
            is_inline=True,
        )
        e.db_id = db["id"]
        e.write_config()
        for n in (1, 2, 3):
            page = e.client.create_page(e.db_id, {
                "Name": _title(f"page{n}"), "Text": _rich(f"text{n}"), "Num": {"number": n * 10},
                "Pick": {"select": {"name": "A"}}, "Tags": {"multi_select": [{"name": "x"}]},
                "When": {"date": {"start": "2026-11-15"}}, "Done": {"checkbox": n == 1},
                "Link": {"url": f"https://example.com/{n}"},
            })
            e.page_ids[f"page{n}"] = page["id"]
        try:
            yield e
        finally:
            try:
                e.client._patch(f"/blocks/{e.db_id}", {"archived": True})
            except Exception as exc:  # leave the database for the user to delete by hand
                print(f"could not trash test database {e.db_id}: {exc}")


def _ok(result):
    assert result.returncode == 0, result.stderr[-3000:]
    assert "Traceback" not in result.stderr, result.stderr[-3000:]


def test_first_sync_stores_every_property_type(env):
    _ok(env.sync())
    assert len(env.query(f'SELECT * FROM "{TABLE}_pages"')) == 3
    r = env.row("page2")
    assert (r["text"], r["num"], r["done"], r["link"]) == ("text2", 20, 0, "https://example.com/2")
    assert (r["pick"], r["tags"], r["when_start"]) == ("A", "x", "2026-11-15")
    assert env.row("page1")["done"] == 1
    props = {r["property_name"] for r in env.query('SELECT property_name FROM "_schema" WHERE table_name = ?', TABLE)}
    assert {"Text", "Num", "Pick", "Tags", "When", "Done", "Link"} <= props
    assert env.query('SELECT value FROM "_meta" WHERE key = ?', f"last_sync:{TABLE}")


def test_edit_is_recorded_in_changes(env):
    env.client.update_page_properties(env.page_ids["page2"], {"Text": _rich("edited")})
    _ok(env.sync())
    assert env.row("page2")["text"] == "edited"
    # first sight of a page is also logged, with old_value NULL; only real edits have an old value
    changes = env.query(f'SELECT * FROM "{TABLE}_changes" WHERE field = ? AND old_value IS NOT NULL', "text")
    assert [(c["old_value"], c["new_value"]) for c in changes] == [("text2", "edited")]


def test_property_rename_moves_the_column(env):
    env.client.patch_database(env.db_id, properties={"Pick": {"name": "Choice"}})
    _ok(env.sync())
    cols = env.columns()
    assert "choice" in cols and "pick" not in cols
    assert env.row("page1")["choice"] == "A"


def test_quiet_period_so_pages_are_not_refetched(env):
    # Incremental sync re-fetches pages edited within 60 s of the previous sync, and Notion reports edit times
    # rounded down to the minute, so wait out two minutes. After a sync past that, no page is re-fetched, which
    # is the state the schema changes below need (the laptop crash case).
    time.sleep(125)
    _ok(env.sync())
    result = env.sync()
    _ok(result)
    assert "0 page(s) found" in result.stderr


def test_type_change_backs_up_old_values(env):
    env.client.patch_database(env.db_id, properties={"Num": {"rich_text": {}}})
    _ok(env.sync())
    backups = env.query('SELECT backup_column FROM "_backups" WHERE table_name = ? AND column_name = ?', TABLE, "num")
    assert len(backups) == 1
    assert env.row("page3")[backups[0]["backup_column"]] == 30
    env.__dict__["num_backup"] = backups[0]["backup_column"]


def test_second_type_change_without_page_edits_does_not_crash(env):
    # The column for the new type was never created (no page re-fetched), so there is nothing to back up.
    env.client.patch_database(env.db_id, properties={"Num": {"checkbox": {}}})
    result = env.sync()
    _ok(result)
    assert "Nothing was backed up" in result.stderr


def test_deleted_property_keeps_its_data(env):
    env.client.patch_database(env.db_id, properties={"Link": None})
    result = env.sync()
    _ok(result)
    assert "no longer exists" in result.stderr
    assert env.row("page1")["link"] == "https://example.com/1"


def test_editing_a_page_keeps_backup_and_orphan_columns(env):
    env.client.update_page_properties(env.page_ids["page1"], {"Text": _rich("edited again")})
    _ok(env.sync())
    r = env.row("page1")
    assert r["text"] == "edited again"
    assert r["link"] == "https://example.com/1"
    assert r[env.__dict__["num_backup"]] == 10


def test_trashed_page_is_marked_by_detect_deleted(env):
    env.client.archive_page(env.page_ids["page3"])
    _ok(env.sync("--detect-deleted"))
    r = env.row("page3")
    assert (r["delete_state"], r["deleted_at"] is not None) == ("trashed", True)
    assert env.row("page1")["deleted_at"] is None


def test_restored_page_is_unmarked(env):
    env.client._patch(f"/pages/{env.page_ids['page3']}", {"archived": False})
    _ok(env.sync("--detect-deleted"))
    r = env.row("page3")
    assert (r["delete_state"], r["deleted_at"]) == (None, None)


def test_full_sync_keeps_all_rows(env):
    _ok(env.sync("--full"))
    assert len(env.query(f'SELECT * FROM "{TABLE}_pages"')) == 3
    assert env.query("PRAGMA integrity_check")[0][0] == "ok"

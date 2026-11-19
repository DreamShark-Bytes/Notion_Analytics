"""
sync.py
Main entry point for the Notion → Power BI sync.

Usage:
    python sync.py                   # one-shot sync using config.toml
    python sync.py --config my.toml  # use a different config file
    python sync.py --full            # ignore last-sync timestamp, fetch all pages
    python sync.py --detect-deleted  # normal sync + mark pages trashed/purged/moved out (nightly)
"""

import argparse
import logging
import re
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Python < 3.11 detected: install tomli with 'pip install tomli'")

from notion_api import NotionClient
from extractor import (
    extract_page_row, extract_comments, extract_content, sanitize_col, is_ignored_property, _SKIP_TYPES,
)
from storage import Storage, physical_columns, _SYSTEM_COLS, _DELETION_COLS
from change_tracker import detect_changes

def _log_handlers(log_path: str = "notion_analytics.log") -> list[logging.Handler]:
    """
    Console and file handlers that cannot fail on a character the locale encoding lacks.
    Windows defaults to cp1252, which has no arrow or emoji (property names can hold emoji).
    """
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    return [logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s",
    handlers=_log_handlers(),
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Config loading
# ------------------------------------------------------------------ #

_OPTION_TYPES = {"select", "status", "multi_select"}


def _upsert_options(
    table: str,
    property_id: str,
    prop_type: str,
    schema: dict,
    storage: Storage,
    now: str,
):
    if prop_type in ("select", "multi_select"):
        for opt in schema.get(prop_type, {}).get("options", []):
            storage.upsert_option(table, property_id, opt["id"], opt["name"], None, now)
    elif prop_type == "status":
        status_def = schema.get("status", {})
        opt_group = {}
        for g in status_def.get("groups", []):
            for oid in g.get("option_ids", []):
                opt_group[oid] = g["name"]
        for opt in status_def.get("options", []):
            storage.upsert_option(
                table, property_id, opt["id"], opt["name"], opt_group.get(opt["id"]), now
            )


def _do_backup(
    table: str,
    col: str,
    prop_name: str,
    stored_type: str,
    prop_type: str,
    storage: Storage,
    now: str,
):
    date_str = now[:10].replace("-", "")
    backup_name = storage.backup_column(table, col, date_str)
    if backup_name is None:
        logger.warning(
            f"[{table}] Property '{prop_name}' changed type "
            f"'{stored_type}' -> '{prop_type}', but {table}_pages has no column '{col}' "
            f"(never created, or a date property stored as '{col}_start'/'{col}_end'). "
            f"Nothing was backed up; the stored type was updated."
        )
        return
    storage.record_backup(table, col, backup_name, stored_type, now)
    logger.warning(
        f"[{table}] Property '{prop_name}' changed type "
        f"'{stored_type}' -> '{prop_type}'. "
        f"Old data preserved in '{backup_name}' (kept indefinitely)."
    )


_RESERVED_COLS = set(_SYSTEM_COLS) | set(_DELETION_COLS)
_RETIRED_RE = re.compile(r"_del_\d{8}(_\d+)?$")


def _free_column(wanted: str, prop_type: str | None, blocked: set[str]) -> str:
    """wanted, or wanted_2, wanted_3 ... : the first base name whose physical columns are all unblocked."""
    candidate, n = wanted, 1
    while any(c in blocked for c in physical_columns(candidate, prop_type)):
        n += 1
        candidate = f"{wanted}_{n}"
    return candidate


def _retire_orphan(table: str, property_id: str, entry: dict, blocked: set[str], storage: Storage, now: str) -> None:
    """Rename a deleted property's column(s) to <col>_del_YYYYMMDD so a live property can take the name."""
    old_col = entry["column_name"]
    new_col = _free_column(f"{old_col}_del_{now[:10].replace('-', '')}", entry["property_type"], blocked)
    storage.rename_property_columns(
        table, old_col, new_col, entry["property_type"], now, property_id=property_id,
        kind="orphan_column_renamed", old_name=entry["property_name"], new_name=entry["property_name"],
        detail="the property no longer exists in Notion; its column was renamed to free the name",
    )
    logger.warning(
        f"[{table}] Column '{old_col}' belonged to a property that no longer exists in Notion; "
        f"renamed it to '{new_col}' because a live property needs the name."
    )
    entry["column_name"] = new_col


def _compare_schema(
    table: str,
    notion_schema: dict,
    storage: Storage,
    now: str,
    include_cols: list[str] = (),
    exclude_cols: list[str] = (),
    col_map: dict | None = None,
) -> set[str]:
    """
    Compare the current Notion schema against the stored _schema table.

    Properties that include_columns / exclude_columns tell the sync to ignore are skipped:
    they never get a column, so they are not tracked, backed up, or reported as removed.

    A property is followed by its id. A rename moves all of its physical columns and its _changes history in
    one transaction (storage.rename_property_columns). A new or renamed property whose column name is taken
    gets name_2, name_3 ...; a deleted property's column is renamed <col>_del_YYYYMMDD only when a live
    property needs its name. Type changes rename the old column to col_bak_YYYYMMDD. Every event is appended
    to _schema_changes. Logs WARNING for properties that have disappeared.

    Returns the set of _id shadow column names for select/status/multi_select fields. When `col_map` is given
    it is filled with {property name: column name} for the live properties (what extract_page_row must use).
    """
    storage.ensure_schema_changes_table()
    stored = storage.get_stored_schema(table)
    notion_props = notion_schema.get("properties", {})
    live_ids = {
        s["id"] for s in notion_props.values() if s.get("type") not in _SKIP_TYPES and s.get("id")
    }
    orphans = {pid: e for pid, e in stored.items() if pid not in live_ids}
    id_shadow_cols: set[str] = set()
    existing_cols = storage.get_column_names(table)

    def claimed_by(col: str) -> str | None:
        for pid, e in stored.items():
            if col == e["column_name"] or col in physical_columns(e["column_name"], e["property_type"]):
                return pid
        return None

    def taken_names(own: set[str]) -> set[str]:
        names = existing_cols | _RESERVED_COLS
        for e in stored.values():
            names |= {e["column_name"], *physical_columns(e["column_name"], e["property_type"])}
        return names - own

    def resolve_column(wanted: str, prop_type: str, own: set[str]) -> str:
        """A free base column for this property; retires deleted properties' columns that hold the wanted name."""
        conflicts = ({wanted, *physical_columns(wanted, prop_type)} - own) & taken_names(own)
        if not conflicts:
            return wanted
        owners = {c: claimed_by(c) for c in conflicts}
        if all(c not in _RESERVED_COLS and owners[c] in orphans for c in conflicts):
            for pid in sorted({owners[c] for c in conflicts}):
                _retire_orphan(table, pid, orphans[pid], taken_names(own), storage, now)
            existing_cols.clear()
            existing_cols.update(storage.get_column_names(table))
            return wanted
        return _free_column(wanted, prop_type, taken_names(own))

    for prop_name, schema in notion_props.items():
        prop_type = schema.get("type")
        if prop_type in _SKIP_TYPES:
            continue
        property_id = schema.get("id")
        if not property_id:
            continue

        if is_ignored_property(prop_name, include_cols, exclude_cols):
            continue

        wanted = sanitize_col(prop_name)

        if property_id in stored:
            entry = stored[property_id]
            stored_col = entry["column_name"]
            stored_name = entry["property_name"]
            stored_type = entry["property_type"]
            col = stored_col

            if prop_name != stored_name or _RETIRED_RE.search(stored_col):
                own = {stored_col, *physical_columns(stored_col, stored_type)}
                wanted_cols = set(physical_columns(wanted, stored_type))
                claimed_elsewhere = set().union(*(
                    set(physical_columns(e["column_name"], e["property_type"]))
                    for pid, e in stored.items() if pid != property_id
                ))
                # Some columns were already renamed by an interrupted run: finish the job (rest, history, _schema).
                resuming = any(
                    old_c not in existing_cols and new_c in existing_cols
                    for old_c, new_c in zip(physical_columns(stored_col, stored_type),
                                            physical_columns(wanted, stored_type))
                ) and not (wanted_cols & claimed_elsewhere)
                if wanted == stored_col or resuming:
                    col = wanted
                else:
                    col = resolve_column(wanted, stored_type, own)
                if col != stored_col:
                    logger.info(
                        f"[{table}] Property renamed in Notion: '{stored_name}' -> '{prop_name}' "
                        f"(column: '{stored_col}' -> '{col}'). Migrating data automatically."
                    )
                    storage.rename_property_columns(
                        table, stored_col, col, stored_type, now, property_id=property_id,
                        kind="property_renamed", old_name=stored_name, new_name=prop_name,
                        detail=None if col == wanted else f"'{wanted}' was taken; used '{col}'",
                    )
                    entry["column_name"] = col
                elif prop_name != stored_name:
                    storage.log_schema_change(
                        table, "property_renamed", now, property_id=property_id, old_name=stored_name,
                        new_name=prop_name, old_column=stored_col, new_column=stored_col,
                        old_type=stored_type, new_type=stored_type, detail="column name unchanged",
                    )

            if prop_type != stored_type:
                existing_backup = storage.get_backup_for_type(table, col, prop_type)
                if existing_backup:
                    restored = storage.restore_backup(table, col, existing_backup)
                    if restored:
                        logger.info(
                            f"[{table}] Property '{prop_name}' type reverted "
                            f"'{stored_type}' -> '{prop_type}'. Restored from '{existing_backup}'."
                        )
                    else:
                        _do_backup(table, col, prop_name, stored_type, prop_type, storage, now)
                else:
                    _do_backup(table, col, prop_name, stored_type, prop_type, storage, now)
                storage.log_schema_change(
                    table, "type_changed", now, property_id=property_id, old_name=prop_name, new_name=prop_name,
                    old_column=col, new_column=col, old_type=stored_type, new_type=prop_type,
                )
        else:
            col = resolve_column(wanted, prop_type, set())
            storage.log_schema_change(
                table, "property_added", now, property_id=property_id, new_name=prop_name, new_column=col,
                new_type=prop_type, detail=None if col == wanted else f"'{wanted}' was taken; used '{col}'",
            )

        storage.upsert_schema(table, property_id, prop_name, col, prop_type, now)
        stored[property_id] = {
            "property_id": property_id, "property_name": prop_name, "column_name": col, "property_type": prop_type,
        }
        if col_map is not None:
            col_map[prop_name] = col

        if prop_type in _OPTION_TYPES:
            id_shadow_cols.add(col + "_id")
            _upsert_options(table, property_id, prop_type, schema, storage, now)

    for property_id, entry in orphans.items():
        if property_id in live_ids:
            continue
        logger.warning(
            f"[{table}] Column '{entry['column_name']}' (Notion property '{entry['property_name']}') "
            f"no longer exists in the Notion schema — it may have been deleted or renamed. "
            f"The column and its history are kept in SQLite and no longer updated."
        )
        if not storage.has_schema_change(table, property_id, "property_removed"):
            storage.log_schema_change(
                table, "property_removed", now, property_id=property_id, old_name=entry["property_name"],
                old_column=entry["column_name"], old_type=entry["property_type"],
            )

    return id_shadow_cols


def _expand_change_fields(fields: list[str], sample_row: dict) -> list[str]:
    """
    Expand sanitized date field names to their _start/_end split variants.

    When a Notion date column (e.g. "due_date") has been migrated to
    "due_date_start" / "due_date_end", the base name no longer appears in
    page rows. This maps the base name to whichever split variants exist so
    change tracking continues working without any config change.
    """
    if not fields:
        return fields
    expanded = []
    for f in fields:
        if f in sample_row:
            expanded.append(f)
        elif f"{f}_start" in sample_row or f"{f}_end" in sample_row:
            if f"{f}_start" in sample_row:
                expanded.append(f"{f}_start")
            if f"{f}_end" in sample_row:
                expanded.append(f"{f}_end")
        else:
            expanded.append(f)
    return expanded


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _load_token(cfg: dict, config_path: str, secrets_path: str | None = None) -> str | None:
    """
    The token comes from config_secrets.toml next to the config file (or --secrets PATH).
    A `token` still in config.toml is used with a warning, so an old setup keeps working until it is moved.
    """
    path = Path(secrets_path) if secrets_path else Path(config_path).resolve().parent / "config_secrets.toml"
    if path.exists():
        token = load_config(str(path)).get("token")
        if token:
            return token
    if cfg.get("token"):
        logger.warning(f"The token is read from {config_path}; move it to {path.name} (see docs/setup.md)")
        return cfg["token"]
    return None


def _db_cfg(db: dict, key: str, default=None):
    return db.get(key, default)


# ------------------------------------------------------------------ #
#  Deleted / moved page detection
# ------------------------------------------------------------------ #

_MAX_PAGE_LOG_LINES = 10  # per-page delete lines logged per database per run; the rest get one summary line
_GUARD_MIN_MISSING = 10  # the ratio guard only applies once at least this many pages look missing


def _norm_id(value: str | None) -> str:
    return (value or "").replace("-", "").lower()


def _normalize_database_cfgs(databases: list[dict]) -> None:
    """Ensure every [[databases]] block has `database_id`; the legacy `id` key is still accepted."""
    for db_cfg in databases:
        if "database_id" not in db_cfg:
            if "id" not in db_cfg:
                logger.error(f"[[databases]] entry '{db_cfg.get('name')}' has no database_id")
                sys.exit(1)
            logger.warning(f"[[databases]] '{db_cfg.get('name')}' uses the legacy key 'id'; rename it to 'database_id'")
            db_cfg["database_id"] = db_cfg["id"]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parent_source_id(page: dict) -> str | None:
    """
    ID of the database / data source a page lives in, or None if its parent is
    something else (a regular page, the workspace). This is the one place that
    knows how a page names its parent, so a switch to data source IDs changes here only.
    """
    parent = page.get("parent") or {}
    return parent.get("data_source_id") or parent.get("database_id")


def _find_configured_table(page: dict, db_cfgs: list[dict]) -> str | None:
    """Config name of the synced database the page now lives in, if any."""
    parent_id = _norm_id(_parent_source_id(page))
    if not parent_id:
        return None
    for cfg in db_cfgs:
        if _norm_id(cfg.get("database_id")) == parent_id:
            return cfg["name"]
    return None


def _classify_missing(client: NotionClient, page_id: str, db_cfgs: list[dict]) -> tuple[str, str | None] | None:
    """
    Decide why a stored page no longer appears in its database query.
    Returns (state, moved_to), or None if it cannot be decided this run
    (transient API error, or the page still reports this database as its parent).
      trashed — page is in Notion's trash (recoverable until Notion purges it)
      gone    — 404: permanently purged, or the integration lost access
      moved   — page exists and is not trashed, but lives somewhere else
    """
    try:
        page = client.get_page(page_id)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return "gone", None
        logger.warning(f"Could not check page {page_id}: {e}; will retry next run")
        return None
    if page.get("archived") or page.get("in_trash"):
        return "trashed", None
    return "moved", _find_configured_table(page, db_cfgs)


def _reconcile_deleted(
    client: NotionClient,
    db_cfg: dict,
    storage: Storage,
    notion_ids: set[str],
    db_cfgs: list[dict],
    now: str,
    track_changes: bool,
) -> dict:
    """
    Compare every ID Notion returned for this database against what is stored.
    Stored pages missing from Notion get a delete_state; already-deleted pages whose
    state changed (trashed → gone) are updated. Returns counts for logging.
    Restores (page reappears) are handled in sync_database's page loop.
    """
    table = db_cfg["name"]
    guard_ratio: float = _db_cfg(db_cfg, "delete_guard_ratio", 0.5)
    result = {"marked": 0, "updated": 0, "skipped": 0, "aborted": False}

    if not storage.ensure_deletion_columns(table):
        return result

    live_ids = storage.get_live_page_ids(table)
    deleted = storage.get_deleted_pages(table)
    missing_live = live_ids - notion_ids
    recheck = {pid for pid, state in deleted.items() if state == "trashed" and pid not in notion_ids}

    if guard_ratio > 0 and live_ids:
        too_many = (
            len(missing_live) >= _GUARD_MIN_MISSING
            and len(notion_ids) < guard_ratio * len(live_ids)
        )
        if not notion_ids or too_many:
            logger.error(
                f"[{table}] Deleted-page detection aborted: Notion returned {len(notion_ids)} page(s) "
                f"but {len(live_ids)} are stored ({len(missing_live)} look missing). This looks like "
                f"lost access or an API problem, not real deletions. Nothing was marked. "
                f"To override, set delete_guard_ratio = 0 for this database."
            )
            result["aborted"] = True
            return result

    logged = 0
    for page_id in sorted(missing_live | recheck):
        verdict = _classify_missing(client, page_id, db_cfgs)
        if verdict is None:
            result["skipped"] += 1
            continue
        state, moved_to = verdict
        if moved_to == table:
            logger.warning(f"[{table}] Page {page_id} is missing from the query but still lives here; skipping")
            result["skipped"] += 1
            continue
        old_state = deleted.get(page_id)
        if state == old_state:
            continue
        storage.mark_deleted(table, page_id, state, moved_to, now)
        if track_changes:
            storage.record_change(table, page_id, "delete_state", old_state, state, now, now)
        result["marked" if old_state is None else "updated"] += 1
        if logged < _MAX_PAGE_LOG_LINES:
            detail = f" -> {moved_to}" if moved_to else ""
            logger.info(f"[{table}] Page {page_id}: {old_state or 'live'} -> {state}{detail}")
            logged += 1

    changed = result["marked"] + result["updated"]
    if changed > logged:
        logger.info(
            f"[{table}] {changed - logged} more page(s) changed delete state; "
            f"see the delete_state column in {table}_pages and the {table}_changes table"
        )

    return result


# ------------------------------------------------------------------ #
#  Per-database sync
# ------------------------------------------------------------------ #

def sync_database(
    client: NotionClient,
    db_cfg: dict,
    storage: Storage,
    full: bool,
    content_fetch_delay_ms: int = 0,
    detect_deleted: bool = False,
    all_db_cfgs: list[dict] | None = None,
) -> bool:
    """Sync one database. Returns False if it could not be synced (schema fetch or query failed)."""
    db_id = db_cfg["database_id"]
    table = db_cfg["name"]
    include_cols: list[str] = _db_cfg(db_cfg, "include_columns", [])
    exclude_cols: list[str] = _db_cfg(db_cfg, "exclude_columns", [])
    include_content: bool = _db_cfg(db_cfg, "include_content", True)
    include_comments: bool = _db_cfg(db_cfg, "include_comments", True)
    track_changes: bool = _db_cfg(db_cfg, "track_changes", True)
    change_fields: list[str] = _db_cfg(db_cfg, "change_fields", [])
    exclude_change_fields: list[str] = _db_cfg(db_cfg, "exclude_change_fields", [])

    # Apply any declared column renames before syncing new data
    renames: dict[str, str] = _db_cfg(db_cfg, "column_renames", {})
    for old_name, new_name in renames.items():
        old_col = sanitize_col(old_name)
        new_col = sanitize_col(new_name)
        storage.apply_column_rename(table, old_col, new_col)

    logger.info(f"[{table}] Fetching schema for database {db_id}")
    try:
        db_schema = client.get_database(db_id)
    except Exception as e:
        logger.error(f"[{table}] Failed to fetch database schema: {e}")
        return False

    now = datetime.now(timezone.utc).isoformat()

    storage.ensure_schema_table()
    storage.ensure_options_table()
    storage.ensure_backups_table()
    storage.ensure_meta_table()
    col_map: dict[str, str] = {}
    id_shadow_cols = _compare_schema(table, db_schema, storage, now, include_cols, exclude_cols, col_map)

    meta_key = f"last_sync:{table}"
    last_sync = None if full else storage.get_meta(meta_key)

    cutoff = None
    if last_sync:
        cutoff = (datetime.fromisoformat(last_sync) - timedelta(seconds=60)).isoformat()
        filter_payload = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"on_or_after": cutoff},
        }
        logger.info(f"[{table}] Incremental sync from {cutoff}")
    else:
        filter_payload = None
        logger.info(f"[{table}] Full sync (no prior timestamp)")

    # Deleted-page detection needs every ID Notion has, so query unfiltered and
    # narrow to the pages worth fetching in memory (same rule the API filter applies,
    # plus pages we don't hold as live: new, moved in, or restored from the trash).
    logger.info(f"[{table}] Querying pages ...")
    try:
        pages = client.query_database(db_id, filter_payload=None if detect_deleted else filter_payload)
    except Exception as e:
        logger.error(f"[{table}] Failed to query database: {e}")
        return False

    notion_ids = {p["id"] for p in pages}
    if detect_deleted and cutoff:
        cutoff_dt = datetime.fromisoformat(cutoff)
        live_ids = storage.get_live_page_ids(table)
        pages = [
            p for p in pages
            if p["id"] not in live_ids or _parse_ts(p["last_edited_time"]) >= cutoff_dt
        ]

    logger.info(f"[{table}] {len(pages)} page(s) found")

    if track_changes:
        storage.ensure_changes_table(table)
        storage.backfill_option_names(table)
    if include_comments:
        storage.ensure_comments_table(table)

    # Sanitize change_fields / exclude_change_fields to match stored col names
    change_fields_san = [sanitize_col(f) for f in change_fields]
    exclude_change_fields_san = list(
        set(sanitize_col(f) for f in exclude_change_fields) | id_shadow_cols
    )

    pages_synced = 0
    changes_recorded = 0
    comments_synced = 0
    _fields_expanded = False
    total_pages = len(pages)

    for page in pages:
        page_id = page["id"]

        # --- Build the page row ---
        row = extract_page_row(page, db_schema, include_cols, exclude_cols, col_map=col_map)

        if include_content:
            row["content_text"] = extract_content(client, page_id)
            if content_fetch_delay_ms > 0:
                time.sleep(content_fetch_delay_ms / 1000)

        # --- Ensure table schema covers all columns in this row ---
        storage.ensure_pages_table(table, row)

        # --- Expand change_fields for split date columns (once per database) ---
        if track_changes and not _fields_expanded:
            expanded_cf = _expand_change_fields(change_fields_san, row)
            expanded_ex = _expand_change_fields(exclude_change_fields_san, row)
            if expanded_cf != change_fields_san or expanded_ex != exclude_change_fields_san:
                logger.debug(f"[{table}] change_fields expanded: {change_fields_san} -> {expanded_cf}")
            change_fields_san = expanded_cf
            exclude_change_fields_san = expanded_ex
            _fields_expanded = True

        # --- Change tracking ---
        if track_changes:
            prev_row = storage.get_page(table, page_id)
            changes = detect_changes(
                row,
                prev_row,
                change_fields_san,
                exclude_change_fields_san,
            )
            for ch in changes:
                field = ch["field"]
                storage.record_change(
                    table,
                    ch["page_id"],
                    field,
                    ch["old_value"],
                    ch["new_value"],
                    ch["valid_from"],
                    ch["detected_at"],
                    old_value_id=prev_row.get(f"{field}_id") if prev_row else None,
                    new_value_id=row.get(f"{field}_id"),
                )
            changes_recorded += len(changes)

            # A page we had marked deleted is back (restored, or moved in from elsewhere).
            # upsert_page clears its delete columns; record that it came back.
            if prev_row and prev_row.get("deleted_at"):
                storage.record_change(
                    table, page_id, "delete_state", prev_row.get("delete_state"), None, now, now
                )
                changes_recorded += 1
                logger.info(f"[{table}] Page {page_id} is back (was {prev_row.get('delete_state')})")

        # --- Upsert page ---
        storage.upsert_page(table, row)
        pages_synced += 1

        # --- Comments ---
        if include_comments:
            comments = extract_comments(client, page_id)
            for comment in comments:
                storage.upsert_comment(table, comment)
            comments_synced += len(comments)

        # --- In-place terminal progress (stdout only, not logged) ---
        pct = int(pages_synced / total_pages * 100) if total_pages else 0
        print(
            f"\r  [{table}] {pages_synced}/{total_pages} pages ({pct}%)"
            f" | {changes_recorded} changes | {comments_synced} comments",
            end="",
            flush=True,
        )

    print()  # move past the progress line

    if detect_deleted:
        outcome = _reconcile_deleted(
            client, db_cfg, storage, notion_ids, all_db_cfgs or [db_cfg], now, track_changes
        )
        logger.info(
            f"[{table}] Deleted-page check: {outcome['marked']} newly marked, "
            f"{outcome['updated']} updated, {outcome['skipped']} skipped"
            + (" (ABORTED by safety guard)" if outcome["aborted"] else "")
        )

    storage.set_meta(meta_key, now)
    logger.info(
        f"[{table}] Done. "
        f"{pages_synced} pages synced, "
        f"{changes_recorded} changes recorded, "
        f"{comments_synced} comments synced."
    )
    return True


# ------------------------------------------------------------------ #
#  Main
# ------------------------------------------------------------------ #

def _publish_snapshot(db_path: str, snapshot_dir: str, keep: int) -> bool:
    """Write a verified snapshot of the database for another machine to fetch. Never raises."""
    sys.path.insert(0, str(Path(__file__).parent / "tools"))
    try:
        from db_snapshot import make_snapshot
        path = make_snapshot(Path(db_path), Path(snapshot_dir), keep)
    except Exception as e:  # SnapshotError, or the tool cannot be imported
        logger.error(f"Snapshot failed: {e}")
        return False
    logger.info(f"Snapshot written: {path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Notion to Power BI sync")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--secrets", default=None, help="Path to the secrets file (default: config_secrets.toml next to --config)")
    parser.add_argument("--full", action="store_true", help="Full refresh (fetch all pages)")
    parser.add_argument(
        "--detect-deleted",
        action="store_true",
        help="Also find pages that were trashed, purged or moved out of each database "
             "(slower: lists every page ID; intended for the nightly run)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    token = _load_token(cfg, args.config, args.secrets)
    if not token:
        logger.error("No Notion token found: set `token` in config_secrets.toml next to the config file")
        sys.exit(1)
    output_cfg = cfg.get("output", {})
    db_path = output_cfg.get("db_path", "notion_powerbi.db")
    export_csv = output_cfg.get("export_csv", False)
    csv_dir = output_cfg.get("csv_dir", "exports")
    content_fetch_delay_ms: int = output_cfg.get("content_fetch_delay_ms", 0)
    snapshot_dir = output_cfg.get("snapshot_dir")  # optional: publish a snapshot after every run
    snapshot_keep: int = output_cfg.get("snapshot_keep", 3)

    databases = cfg.get("databases", [])
    if not databases:
        logger.error("No databases configured in config.toml")
        sys.exit(1)
    _normalize_database_cfgs(databases)

    client = NotionClient(token)
    storage = Storage(db_path)

    start = datetime.now(timezone.utc)
    logger.info(f"Sync started at {start.isoformat()}")
    logger.info(f"Output: {db_path}")

    failed: list[str] = []
    for db_cfg in databases:
        try:
            ok = sync_database(
                client, db_cfg, storage, full=args.full,
                content_fetch_delay_ms=content_fetch_delay_ms,
                detect_deleted=args.detect_deleted, all_db_cfgs=databases,
            )
        except Exception as e:
            logger.error(f"Unexpected error syncing '{db_cfg.get('name')}': {e}", exc_info=True)
            ok = False
        if not ok:
            failed.append(str(db_cfg.get("name")))

        if export_csv:
            storage.export_csv(db_cfg["name"], csv_dir)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    storage.close()

    snapshot_ok = _publish_snapshot(db_path, snapshot_dir, snapshot_keep) if snapshot_dir else True

    if failed:
        logger.error(f"Sync finished with {len(failed)} failed database(s) in {elapsed:.1f}s: {', '.join(failed)}")
    if failed or not snapshot_ok:
        sys.exit(1)
    logger.info(f"Sync complete in {elapsed:.1f}s")


if __name__ == "__main__":
    main()

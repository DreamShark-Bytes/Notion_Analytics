"""
extractor.py
Converts raw Notion API responses into flat, SQLite-ready dicts.
"""

from __future__ import annotations
import re
import logging
from typing import TYPE_CHECKING

from notion_api import normalize_property, extract_content, extract_comments

if TYPE_CHECKING:
    from notion_api import NotionClient

logger = logging.getLogger(__name__)

# Re-export so sync.py imports don't need to change
__all__ = [
    "sanitize_col",
    "extract_page_row",
    "extract_content",
    "extract_comments",
]

# Notion property types skipped from page rows (not analytically useful)
_SKIP_TYPES = {"created_by", "last_edited_by"}


# ------------------------------------------------------------------ #
#  Column name sanitization (SQLite-specific)
# ------------------------------------------------------------------ #

def sanitize_col(name: str) -> str:
    """Convert a Notion property name to a safe SQLite column name."""
    clean = re.sub(r"[^\w]", "_", name)
    clean = re.sub(r"_+", "_", clean).strip("_").lower()
    if clean and clean[0].isdigit():
        clean = f"_{clean}"
    return clean or "col"


# ------------------------------------------------------------------ #
#  Page row extraction (SQLite-specific)
# ------------------------------------------------------------------ #

def extract_page_row(
    page: dict,
    db_schema: dict,
    include_cols: list[str],
    exclude_cols: list[str],
    files_handling: str = "bool",
) -> dict:
    """
    Return a flat dict of {sanitized_col: value} for a Notion page.

    Always included: page_id, created_time, last_edited_time, url.
    include_cols / exclude_cols apply to Notion property names (original, not sanitized).
    """
    row: dict = {
        "page_id": page["id"],
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "url": page.get("url"),
    }

    schema_props = db_schema.get("properties", {})
    page_props = page.get("properties", {})

    for prop_name, schema in schema_props.items():
        prop_type = schema.get("type")

        if prop_type in _SKIP_TYPES:
            continue
        if include_cols and prop_name not in include_cols:
            continue
        if prop_name in exclude_cols:
            continue

        col = sanitize_col(prop_name)
        prop_value = page_props.get(prop_name, {})

        try:
            if prop_type == "date":
                d = prop_value.get("date")
                row[col + "_start"] = d.get("start") if d else None
                row[col + "_end"] = d.get("end") if d else None
            elif prop_type == "select":
                sel = prop_value.get("select")
                row[col] = sel["name"] if sel else None
                row[col + "_id"] = sel["id"] if sel else None
            elif prop_type == "status":
                s = prop_value.get("status")
                row[col] = s["name"] if s else None
                row[col + "_id"] = s["id"] if s else None
            elif prop_type == "multi_select":
                opts = prop_value.get("multi_select", [])
                row[col] = ", ".join(o["name"] for o in opts) if opts else None
                row[col + "_id"] = ", ".join(o["id"] for o in opts) if opts else None
            else:
                row[col] = normalize_property(prop_type, prop_value, files_handling=files_handling)
        except Exception as e:
            logger.warning(f"Failed to normalize '{prop_name}' ({prop_type}): {e}")
            if prop_type == "date":
                row[col + "_start"] = None
                row[col + "_end"] = None
            elif prop_type in ("select", "status", "multi_select"):
                row[col] = None
                row[col + "_id"] = None
            else:
                row[col] = None

    return row

/*
  stg_tasks
  ---------
  Casts date and timestamp columns from Notion's ISO 8601 text format to
  native DuckDB types. All other columns pass through unchanged, so new
  Notion properties appear here automatically on the next sync without a
  model change.

  Date casting strategy: try_cast(x as timestamptz)::date
  Notion returns two date formats depending on whether the user set a time:
    - Plain date:     "2026-08-04"
    - Datetime+tz:    "2026-08-04T14:00:00.000-05:00"
  Casting to timestamptz first handles both; the ::date truncation then
  drops the time component. try_cast returns NULL on any unparseable value
  rather than raising an error.
*/
select * replace(
    -- System timestamps (Notion always emits these as full ISO 8601 + timezone)
    try_cast(created_time     as timestamptz) as created_time,
    try_cast(last_edited_time as timestamptz) as last_edited_time,

    -- Due Date (Notion date property → stored as due_date_start / due_date_end)
    try_cast(due_date_start as timestamptz)::date as due_date_start,
    try_cast(due_date_end   as timestamptz)::date as due_date_end,

    -- Closed Date
    try_cast(closed_date_start as timestamptz)::date as closed_date_start,
    try_cast(closed_date_end   as timestamptz)::date as closed_date_end
)
from {{ source('notion_raw', 'tasks_pages') }}

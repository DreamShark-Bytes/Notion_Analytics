/*
  stg_job_applications
  --------------------
  Job applications from Notion with date columns cast to DATE and boolean
  flags cast to BOOLEAN.

  Schema confirmed from job_applications_pages (ThinkPad, August 2026):
    applied_date_start, follow_up_date_start, follow_up_date_end, last_follow_up
    resume (INTEGER 0/1), cover_letter (INTEGER 0/1)

  Note: if 'last_follow_up' is a formula rather than a Notion date property,
  it may already arrive as a plain date string — try_cast handles that too.
*/
select * replace(
    try_cast(created_time     as timestamptz) as created_time,
    try_cast(last_edited_time as timestamptz) as last_edited_time,

    -- Application date
    try_cast(applied_date_start as timestamptz)::date as applied_date_start,
    try_cast(applied_date_end   as timestamptz)::date as applied_date_end,

    -- Follow-up scheduling
    try_cast(follow_up_date_start as timestamptz)::date as follow_up_date_start,
    try_cast(follow_up_date_end   as timestamptz)::date as follow_up_date_end,

    -- last_follow_up: formula-derived date field
    try_cast(last_follow_up as timestamptz)::date as last_follow_up,

    -- Checkbox columns stored as INTEGER 0/1 by sync.py
    resume::boolean       as resume,
    cover_letter::boolean as cover_letter
)
from {{ source('notion_raw', 'job_applications_pages') }}

/*
  stg_task_changes
  ----------------
  Change history for tasks (Status, Due Date, Closed Date).
  Casts the two timestamp columns to timestamptz; all other columns
  (field name, old/new value, old/new option ID) pass through as TEXT.
*/
select * replace(
    -- valid_from is NULL for the first observed state of a field
    try_cast(valid_from  as timestamptz) as valid_from,
    try_cast(detected_at as timestamptz) as detected_at
)
from {{ source('notion_raw', 'tasks_changes') }}

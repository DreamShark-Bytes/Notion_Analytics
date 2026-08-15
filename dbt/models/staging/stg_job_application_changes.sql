/*
  stg_job_application_changes
  ---------------------------
  Change history for job applications (Status field).
  Casts timestamps; all other columns pass through unchanged.
*/
select * replace(
    try_cast(valid_from  as timestamptz) as valid_from,
    try_cast(detected_at as timestamptz) as detected_at
)
from {{ source('notion_raw', 'job_applications_changes') }}

/*
  stg_pursuits
  ------------
  Pursuits (goals/areas of focus) from Notion.
  Casts system timestamps; all property columns pass through unchanged.
  Expand this model's REPLACE clause as the Pursuits schema is
  fully characterized and additional date columns are identified.
*/
select * replace(
    try_cast(created_time     as timestamptz) as created_time,
    try_cast(last_edited_time as timestamptz) as last_edited_time
)
from {{ source('notion_raw', 'pursuits_pages') }}

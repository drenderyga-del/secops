-- PT AI async scanning moved to Vampy/ASOC orchestration; the secpipe-monitor
-- worker and its queue table were removed from the codebase. Drop the dead
-- table so it does not linger in backups and schema dumps.

DROP TABLE IF EXISTS ptai_scan_requests CASCADE;

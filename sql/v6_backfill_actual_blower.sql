-- v6 R1A backfill: parse "actual_blower=NN" out of notes into the typed
-- column actual_blower_pct_typed. Idempotent — only updates rows where the
-- typed column is currently NULL but the substring is present in notes.
--
-- Sample inspection (run interactively, not part of the batch):
--   SELECT id, notes,
--          (regexp_match(notes, 'actual_blower=([0-9]+)'))[1]::int AS parsed
--   FROM controller_readings
--   WHERE notes ~ 'actual_blower=[0-9]+'
--   LIMIT 5;

BEGIN;

UPDATE controller_readings
SET actual_blower_pct_typed = (regexp_match(notes, 'actual_blower=([0-9]+)'))[1]::int
WHERE actual_blower_pct_typed IS NULL
  AND notes ~ 'actual_blower=[0-9]+';

COMMIT;

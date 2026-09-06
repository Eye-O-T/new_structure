-- person_id identifies a camera-local track. Cross-camera identity is separate.
-- Preserve conflicting legacy values for audit; never infer a global identity.
UPDATE events
SET metadata_json = json_set(metadata_json, '$.legacy_person_id', person_id)
WHERE track_id IS NOT NULL AND person_id IS NOT NULL AND track_id != person_id;

UPDATE events SET person_id = COALESCE(track_id, person_id);
ALTER TABLE events ADD COLUMN global_person_id TEXT;
ALTER TABLE events DROP COLUMN track_id;

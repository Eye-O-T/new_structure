ALTER TABLE cameras ADD COLUMN recording_enabled INTEGER NOT NULL DEFAULT 1 CHECK (recording_enabled IN (0, 1));
ALTER TABLE cameras ADD COLUMN recording_last_error_code TEXT;

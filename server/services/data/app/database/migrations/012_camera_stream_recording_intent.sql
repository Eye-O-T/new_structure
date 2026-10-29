ALTER TABLE cameras ADD COLUMN stream_requested INTEGER NOT NULL DEFAULT 1 CHECK (stream_requested IN (0, 1));

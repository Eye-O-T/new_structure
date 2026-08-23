CREATE TABLE edge_devices (
    edge_device_id TEXT PRIMARY KEY,
    management_url TEXT NOT NULL UNIQUE,
    recovery_url TEXT,
    auth_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE edge_runtime_status (
    edge_device_id TEXT PRIMARY KEY REFERENCES edge_devices(edge_device_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    online INTEGER NOT NULL DEFAULT 0 CHECK (online IN (0, 1)),
    cpu_percent REAL CHECK (cpu_percent IS NULL OR (cpu_percent >= 0 AND cpu_percent <= 100)),
    memory_percent REAL CHECK (memory_percent IS NULL OR (memory_percent >= 0 AND memory_percent <= 100)),
    storage_percent REAL CHECK (storage_percent IS NULL OR (storage_percent >= 0 AND storage_percent <= 100)),
    battery_percent REAL CHECK (battery_percent IS NULL OR (battery_percent >= 0 AND battery_percent <= 100)),
    power_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK (power_source IN ('external', 'battery', 'unknown')),
    last_seen_at TEXT,
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE camera_runtime_status (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(camera_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    camera_input_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (camera_input_status IN ('online', 'offline', 'lost', 'unknown')),
    central_connection_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (central_connection_status IN ('online', 'offline', 'unknown')),
    current_video_profile TEXT NOT NULL DEFAULT 'hd'
        CHECK (current_video_profile IN ('hd', 'fhd')),
    event_cursor TEXT,
    last_seen_at TEXT,
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE camera_video_profiles (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(camera_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    current_profile TEXT NOT NULL DEFAULT 'hd'
        CHECK (current_profile IN ('hd', 'fhd')),
    desired_profile TEXT NOT NULL DEFAULT 'hd'
        CHECK (desired_profile IN ('hd', 'fhd')),
    supported_profiles_json TEXT NOT NULL DEFAULT '["hd"]',
    encoder TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE recovery_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    outage_started_at TEXT NOT NULL,
    outage_ended_at TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('detected', 'waiting_for_recovery', 'downloading', 'indexing', 'completed', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    next_retry_at TEXT,
    last_error TEXT,
    recovery_summary_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(camera_id, outage_started_at)
);

CREATE INDEX idx_recovery_jobs_due
ON recovery_jobs(status, next_retry_at, id);

ALTER TABLE events ADD COLUMN edge_event_id TEXT;

CREATE UNIQUE INDEX idx_events_edge_event_id
ON events(camera_id, edge_event_id) WHERE edge_event_id IS NOT NULL;

INSERT INTO camera_runtime_status(camera_id, updated_at)
SELECT camera_id, updated_at FROM cameras;

INSERT INTO camera_video_profiles(camera_id, created_at, updated_at)
SELECT camera_id, created_at, updated_at FROM cameras;

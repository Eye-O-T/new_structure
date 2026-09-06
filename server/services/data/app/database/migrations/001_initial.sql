CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    stream_path TEXT NOT NULL UNIQUE,
    edge_device_id TEXT,
    source_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'offline'
        CHECK (status IN ('online', 'offline', 'degraded', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE user_camera_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    camera_id INTEGER NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, camera_id)
);

CREATE TABLE recording_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    format TEXT NOT NULL CHECK (format IN ('fmp4', 'mp4', 'mpegts')),
    codec TEXT NOT NULL DEFAULT 'h264',
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    source TEXT NOT NULL CHECK (source IN ('central', 'edge_recovery', 'import')),
    status TEXT NOT NULL
        CHECK (status IN ('writing', 'ready', 'missing', 'corrupt', 'deleting', 'deleted')),
    checksum TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (end_time > start_time)
);

CREATE INDEX idx_segments_camera_time
ON recording_segments(camera_id, start_time, end_time);

CREATE INDEX idx_segments_status_end_time
ON recording_segments(status, end_time);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    person_id TEXT,
    track_id TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    recording_segment_id INTEGER REFERENCES recording_segments(id) ON DELETE SET NULL,
    snapshot_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_events_camera_time
ON events(camera_id, occurred_at);

CREATE INDEX idx_events_camera_time_type
ON events(camera_id, occurred_at, event_type);

CREATE INDEX idx_events_type_time
ON events(event_type, occurred_at);

CREATE TABLE event_recording_segments (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    recording_segment_id INTEGER NOT NULL REFERENCES recording_segments(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (event_id, recording_segment_id)
);

CREATE INDEX idx_event_segments_segment
ON event_recording_segments(recording_segment_id, event_id);

CREATE TABLE refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jti TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL,
    family_id TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    rotated_from_jti TEXT REFERENCES refresh_tokens(jti) ON DELETE SET NULL,
    replaced_by_jti TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_refresh_tokens_user
ON refresh_tokens(user_id, expires_at);

CREATE INDEX idx_refresh_tokens_family
ON refresh_tokens(family_id);

CREATE TABLE revoked_tokens (
    jti TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    reason TEXT
);

CREATE INDEX idx_revoked_tokens_expiry
ON revoked_tokens(expires_at);

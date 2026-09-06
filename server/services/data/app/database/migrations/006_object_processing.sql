CREATE TABLE object_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK(stage IN ('identity','analysis')),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','complete','failed','unconfigured')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_id TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id,stage)
);
CREATE INDEX idx_object_jobs_claim ON object_jobs(stage,state,next_attempt_at);

CREATE TABLE person_identity_links (
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON UPDATE CASCADE ON DELETE CASCADE,
    tracking_session_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    global_person_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(camera_id,tracking_session_id,person_id)
);

CREATE TABLE live_objects (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(camera_id) ON UPDATE CASCADE ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);

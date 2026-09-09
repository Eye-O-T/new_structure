-- 이벤트당 식별·분석 작업을 하나씩 두고 임대 기반으로 중단 작업을 다시 할당한다.
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

-- 재접속 후 로컬 ID 재사용을 구분하도록 카메라·추적 세션·인물 ID를 함께 키로 사용한다.
CREATE TABLE person_identity_links (
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON UPDATE CASCADE ON DELETE CASCADE,
    tracking_session_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    global_person_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(camera_id,tracking_session_id,person_id)
);

-- 최신 좌표는 이벤트 이력과 분리해 카메라당 한 행으로 보관하고 관측·수신 시각을 구별한다.
CREATE TABLE live_objects (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(camera_id) ON UPDATE CASCADE ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);

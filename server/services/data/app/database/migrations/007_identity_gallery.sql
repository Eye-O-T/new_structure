-- 전처리기의 재전송도 Edge 이벤트와 독립된 키로 중복 저장·후속 작업 생성을 막는다.
ALTER TABLE events ADD COLUMN source_event_id TEXT;
CREATE UNIQUE INDEX idx_events_source_event_id
    ON events(camera_id, source_event_id) WHERE source_event_id IS NOT NULL;

-- 특징 벡터는 공개 이벤트 metadata와 분리한다. 각 특징 공간에서 track당 최신 표본만 둔다.
CREATE TABLE identity_gallery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    global_person_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON UPDATE CASCADE ON DELETE CASCADE,
    tracking_session_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 16 AND 2048),
    features_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(space_id, camera_id, tracking_session_id, person_id)
);
CREATE INDEX idx_identity_gallery_candidates
    ON identity_gallery(space_id, dimensions, observed_at);
CREATE INDEX idx_identity_gallery_observed ON identity_gallery(observed_at);

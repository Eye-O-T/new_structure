-- 동시 track 관측 구간은 gallery 표본 제거 뒤에도 보관 정책 기간 동안 유지한다.
CREATE TABLE person_track_presence (
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON UPDATE CASCADE ON DELETE CASCADE,
    tracking_session_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at TEXT,
    PRIMARY KEY(camera_id, tracking_session_id, person_id)
);
CREATE INDEX idx_track_presence_camera_time
    ON person_track_presence(camera_id, first_seen_at, last_seen_at);
INSERT INTO person_track_presence
    (camera_id, tracking_session_id, person_id, first_seen_at, last_seen_at, ended_at)
SELECT camera_id, json_extract(metadata_json,'$.tracking_session_id'), person_id,
       MIN(occurred_at), MAX(occurred_at),
       CASE WHEN MAX(CASE WHEN event_type='person_disappeared' THEN occurred_at END)
                     = MAX(occurred_at)
            THEN MAX(occurred_at) END
FROM events
WHERE person_id IS NOT NULL
  AND json_extract(metadata_json,'$.tracking_session_id') IS NOT NULL
  AND event_type IN ('person_appeared','person_disappeared')
GROUP BY camera_id, json_extract(metadata_json,'$.tracking_session_id'), person_id;

CREATE INDEX idx_events_retention ON events(created_at, occurred_at, id);
CREATE INDEX idx_events_track ON events(camera_id, person_id, json_extract(metadata_json,'$.tracking_session_id'));
CREATE INDEX idx_events_snapshot ON events(snapshot_path);
CREATE INDEX idx_events_crop ON events(json_extract(metadata_json,'$.object.crop_path'));
CREATE INDEX idx_events_boxed ON events(json_extract(metadata_json,'$.object.annotated_snapshot_path'));
CREATE INDEX idx_object_jobs_retention ON object_jobs(state, created_at, updated_at);
CREATE INDEX idx_links_retention ON person_identity_links(created_at);

-- 파일 점검은 디렉터리 순회 위치를 기록하고 다음 실행·재시작에서 이어 간다.
CREATE TABLE maintenance_cursors (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

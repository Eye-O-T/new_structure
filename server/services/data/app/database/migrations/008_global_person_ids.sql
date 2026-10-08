-- 전역 인물 ID는 카메라·추적 세션과 무관한 단일 자동 증가 정수다.
CREATE TABLE global_persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);

-- 이미 문자열 ID를 사용하던 DB도 기존 연결을 잃지 않도록 하나씩 정수 ID를 배정한다.
INSERT INTO global_persons(legacy_id, created_at)
SELECT global_person_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM (
    SELECT global_person_id FROM events WHERE global_person_id IS NOT NULL
    UNION
    SELECT global_person_id FROM person_identity_links
    UNION
    SELECT global_person_id FROM identity_gallery
);

UPDATE events
SET global_person_id = CAST((
    SELECT id FROM global_persons
    WHERE legacy_id = events.global_person_id
) AS INTEGER)
WHERE global_person_id IS NOT NULL;

UPDATE person_identity_links
SET global_person_id = CAST((
    SELECT id FROM global_persons
    WHERE legacy_id = person_identity_links.global_person_id
) AS INTEGER);

UPDATE identity_gallery
SET global_person_id = CAST((
    SELECT id FROM global_persons
    WHERE legacy_id = identity_gallery.global_person_id
) AS INTEGER);
-- access JWT의 sid가 가리키는 로그인 계열을 빠르게 검사한다.
CREATE INDEX idx_refresh_family_owner
    ON refresh_tokens(user_id, COALESCE(family_id,jti), expires_at);

-- 장애 발생 당시의 Edge를 보존한다. 카메라를 새 장치에 연결해도 복구 원본은 바뀌지 않는다.
ALTER TABLE recovery_jobs ADD COLUMN edge_device_id TEXT
    REFERENCES edge_devices(edge_device_id) ON UPDATE CASCADE ON DELETE RESTRICT;
UPDATE recovery_jobs SET edge_device_id=(
    SELECT e.edge_device_id FROM cameras c
    JOIN edge_devices e ON e.edge_device_id=c.edge_device_id
    WHERE c.camera_id=recovery_jobs.camera_id
);
CREATE INDEX idx_recovery_origin ON recovery_jobs(edge_device_id, camera_id);

-- 구형 이력은 현재 남아 있는 장치 연결까지만 복원할 수 있다.
UPDATE events SET metadata_json=json_set(metadata_json,'$.recovery_edge_device_id',(
    SELECT e.edge_device_id FROM cameras c
    JOIN edge_devices e ON e.edge_device_id=c.edge_device_id
    WHERE c.camera_id=events.camera_id
)) WHERE event_type IN ('central_connection_lost','central_connection_restored');
CREATE INDEX idx_events_recovery_origin ON events(
    camera_id,event_type,json_extract(metadata_json,'$.recovery_edge_device_id'),occurred_at
);
CREATE INDEX idx_events_origin_device ON events(
    json_extract(metadata_json,'$.recovery_edge_device_id')
);

-- 단말 토큰은 하나의 등록에만 연결하고 사용자·로그인 계열·역할로 수신 자격을 재검사한다.
CREATE TABLE mobile_devices (
    device_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    token TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL CHECK (platform IN ('android', 'ios')),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    event_types_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_mobile_devices_user ON mobile_devices(user_id, family_id);

-- 이벤트·단말별 발송을 한 번 예약하고 임대·만료·재시도 상태를 DB에 남긴다.
CREATE TABLE push_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    device_id TEXT NOT NULL REFERENCES mobile_devices(device_id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','sending','sent','failed','cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    lease_id TEXT,
    lease_until TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, device_id)
);
CREATE INDEX idx_push_deliveries_due ON push_deliveries(state, next_attempt_at);

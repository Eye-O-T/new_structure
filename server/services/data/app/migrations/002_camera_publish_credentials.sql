CREATE TABLE camera_publish_credentials (
    camera_id TEXT PRIMARY KEY REFERENCES cameras(camera_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_camera_publish_username
ON camera_publish_credentials(username);

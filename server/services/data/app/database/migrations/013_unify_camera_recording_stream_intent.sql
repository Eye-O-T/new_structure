UPDATE cameras
SET recording_enabled = CASE
        WHEN enabled = 1 AND recording_enabled = 1 THEN 1
        ELSE 0
    END,
    stream_requested = CASE
        WHEN enabled = 1 AND recording_enabled = 1 THEN 1
        ELSE 0
    END;

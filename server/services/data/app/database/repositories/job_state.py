"""작업 상태와 공개 이벤트 상태를 같은 트랜잭션에서 변경한다."""

import json
import sqlite3


def update_event_job_state(
    connection: sqlite3.Connection,
    event_id: int,
    stage: str,
    state: str,
    stamp: str,
    *,
    error_code: str | None = None,
) -> None:
    row = connection.execute(
        "SELECT metadata_json FROM events WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        return
    metadata = json.loads(row["metadata_json"])
    result = metadata.get(stage, {}).get("result", {})
    if error_code is not None:
        result = {"error_code": error_code}
    metadata[stage] = {"status": state, "updated_at": stamp, "result": result}
    connection.execute(
        "UPDATE events SET metadata_json=? WHERE id=?",
        (json.dumps(metadata, ensure_ascii=False, allow_nan=False), event_id),
    )

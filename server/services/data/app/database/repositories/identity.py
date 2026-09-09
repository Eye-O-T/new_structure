# 특징 비교와 ID 배정은 완료 요청의 유효 임대 확인 뒤 같은 쓰기 트랜잭션에서만 실행한다.

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import timedelta

from ai_cctv_core.contracts.objects import IdentityDescriptor
from ai_cctv_core.time import format_utc, parse_utc

MATCH_THRESHOLD = 0.97
MATCH_MARGIN = 0.05
MAX_OBSERVATION_GAP_SECONDS = 1800
SAME_CAMERA_EXCLUSION_SECONDS = 30
MAX_GALLERY_SAMPLES = 5000


def resolve_identity(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    session: str,
    descriptor: IdentityDescriptor,
    stamp: str,
) -> tuple[str, dict]:
    """비공개 표본을 비교하고 확정할 ID와 공개 가능한 결정 근거만 반환한다."""
    track = (row["camera_id"], session, row["person_id"])
    existing = connection.execute(
        "SELECT global_person_id FROM person_identity_links "
        "WHERE camera_id=? AND tracking_session_id=? AND person_id=?",
        track,
    ).fetchone()
    observed = parse_utc(row["occurred_at"])
    newest = connection.execute(
        "SELECT MAX(observed_at) FROM identity_gallery"
    ).fetchone()[0]
    # 관측시각 기준으로 정리해 지연 도착한 작업을 처리하며, 역순 작업이 새 표본을 삭제하지 않는다.
    watermark = max(observed, parse_utc(newest)) if newest else observed
    cutoff = format_utc(watermark - timedelta(seconds=MAX_OBSERVATION_GAP_SECONDS))
    connection.execute("DELETE FROM identity_gallery WHERE observed_at<?", (cutoff,))
    _trim_gallery(connection)
    similarity = None
    if existing is not None:
        global_id = existing["global_person_id"]
        decision = "existing_track"
    else:
        # 같은 인물의 여러 표본을 서로 다른 1·2위 후보로 세면 정상 일치도 모호하다고 판단한다.
        # ID별 최고 유사도를 비교하며, 다른 공간의 동시 관측도 동일 카메라 충돌에는 반영한다.
        candidates = connection.execute(
            "SELECT * FROM identity_gallery WHERE observed_at BETWEEN ? AND ? "
            "ORDER BY observed_at DESC,id DESC LIMIT ?",
            (
                format_utc(observed - timedelta(seconds=MAX_OBSERVATION_GAP_SECONDS)),
                format_utc(observed + timedelta(seconds=MAX_OBSERVATION_GAP_SECONDS)),
                MAX_GALLERY_SAMPLES,
            ),
        ).fetchall()
        blocked_ids = {
            candidate["global_person_id"]
            for candidate in candidates
            if candidate["camera_id"] == row["camera_id"]
            and (candidate["tracking_session_id"], candidate["person_id"])
            != (session, row["person_id"])
            and abs((observed - parse_utc(candidate["observed_at"])).total_seconds())
            <= SAME_CAMERA_EXCLUSION_SECONDS
        }
        scores: dict[str, float] = {}
        norm = math.hypot(*descriptor.features)
        for candidate in candidates:
            candidate_id = candidate["global_person_id"]
            if (
                candidate_id in blocked_ids
                or candidate["space_id"] != descriptor.space_id
                or candidate["dimensions"] != len(descriptor.features)
            ):
                continue
            features = json.loads(candidate["features_json"])
            score = math.fsum(
                left * right for left, right in zip(descriptor.features, features)
            ) / (norm * math.hypot(*features))
            score = min(1.0, max(-1.0, score))
            scores[candidate_id] = max(scores.get(candidate_id, -1.0), score)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        similarity = ranked[0][1] if ranked else None
        if (
            ranked
            and ranked[0][1] >= MATCH_THRESHOLD
            and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= MATCH_MARGIN)
        ):
            global_id, decision = ranked[0][0], "matched"
        else:
            global_id, decision = "person-" + uuid.uuid4().hex, "new"

    # 원본 벡터는 이 테이블에만 저장하고 이벤트에는 아래의 요약만 전달한다.
    # 늦게 완료된 같은 track의 과거 작업은 더 최근 특징을 덮어쓰지 않는다.
    if row["occurred_at"] >= cutoff:
        connection.execute(
            "INSERT INTO identity_gallery(global_person_id,space_id,camera_id,"
            "tracking_session_id,person_id,dimensions,features_json,observed_at,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(space_id,camera_id,tracking_session_id,person_id) "
            "DO UPDATE SET features_json=excluded.features_json,"
            "dimensions=excluded.dimensions,observed_at=excluded.observed_at,"
            "updated_at=excluded.updated_at "
            "WHERE excluded.observed_at>identity_gallery.observed_at",
            (
                global_id,
                descriptor.space_id,
                *track,
                len(descriptor.features),
                json.dumps(descriptor.features, allow_nan=False),
                row["occurred_at"],
                stamp,
                stamp,
            ),
        )
        _trim_gallery(connection)
    return global_id, {
        "method": "appearance",
        "decision": decision,
        # 코사인 유사도는 일치 확률이 아니다. 후보가 없거나 기존 track이면 점수가 없다.
        "similarity": round(similarity, 6) if similarity is not None else None,
    }


def _trim_gallery(connection: sqlite3.Connection) -> None:
    # 후보 조회뿐 아니라 실제 저장량도 제한해 긴 가동 시간에 벡터가 무한히 쌓이지 않게 한다.
    connection.execute(
        "DELETE FROM identity_gallery WHERE id IN (SELECT id FROM identity_gallery "
        "ORDER BY observed_at DESC,id DESC LIMIT -1 OFFSET ?)",
        (MAX_GALLERY_SAMPLES,),
    )

# 인물 식별·추가 분석의 작업 대기열, 최신 객체 좌표, 카메라 간 인물 연결을 저장한다.

import json
import uuid
from datetime import timedelta

from ai_cctv_core.contracts.objects import ObjectJobCompletion
from ai_cctv_core.time import format_utc, utc_now

from .identity import resolve_identity


class ObjectRepositoryMixin:
    def requeue_unconfigured_objects(self, stage):
        # 아직 모델이 없는 작업은 실패와 구분한다. 모델 연결 후 이 목록을 다시 대기 상태로 옮긴다.
        now = format_utc(utc_now())
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE object_jobs SET state='pending',attempts=0,next_attempt_at=?,updated_at=? WHERE id IN (SELECT id FROM object_jobs WHERE stage=? AND state='unconfigured' ORDER BY id LIMIT 100)",
                (now, now, stage),
            )
            return result.rowcount

    # 관측 시각이 더 최신인 자료만 덮어써 늦게 도착한 좌표가 화면을 되돌리지 않게 한다.
    def put_live_objects(self, camera_id, payload):
        stamp = format_utc(utc_now())
        observed = format_utc(payload.observed_at)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO live_objects VALUES (?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET payload_json=excluded.payload_json,observed_at=excluded.observed_at,received_at=excluded.received_at WHERE excluded.observed_at>live_objects.observed_at",
                (camera_id, payload.model_dump_json(), observed, stamp),
            )

    # 활성 카메라의 신선한 관찰에 같은 추적 세션의 전역 인물 ID를 결합한다.
    def get_live_objects(self, camera_id):
        from ai_cctv_core.time import parse_utc

        now = utc_now()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT l.* FROM live_objects l JOIN cameras c ON c.camera_id=l.camera_id WHERE l.camera_id=? AND c.enabled=1",
                (camera_id,),
            ).fetchone()
            # 3초보다 오래된 좌표는 화면에 남기지 않는다. 미래 시각도 비정상으로 보고 제외한다.
            if (
                row is None
                or not 0 <= (now - parse_utc(row["observed_at"])).total_seconds() <= 3
            ):
                return {"camera_id": camera_id, "objects": [], "stale": True}
            result = json.loads(row["payload_json"])
            links = {
                r["person_id"]: r["global_person_id"]
                for r in connection.execute(
                    "SELECT person_id,global_person_id FROM person_identity_links WHERE camera_id=? AND tracking_session_id=?",
                    (camera_id, result["tracking_session_id"]),
                ).fetchall()
            }
            for obj in result["objects"]:
                obj["global_person_id"] = links.get(obj["person_id"])
            return {**result, "camera_id": camera_id, "stale": False}

    # 객체 관찰이 있는 이벤트에만 식별·분석 작업을 만들며 이벤트의 트랜잭션을 공유한다.
    def _enqueue_object_jobs(self, connection, event_id, values):
        observation = values.get("object_observation")
        if observation is None:
            return
        now = format_utc(utc_now())
        for stage in ("identity", "analysis"):
            connection.execute(
                "INSERT INTO object_jobs(event_id,stage,next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,?)",
                (event_id, stage, now, now, now),
            )

    # 최대 시도 횟수 안에서 대기 중이거나 임대가 만료된 작업 하나를 원자적으로 할당한다.
    def claim_object_job(self, stage):
        # 임대(lease)는 일정 시간 동안 한 작업을 처리할 권한이다.
        # 작업자가 중단돼도 5분 뒤 다시 가져갈 수 있어 작업이 영원히 멈추지 않는다.
        now = utc_now()
        stamp = format_utc(now)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE object_jobs SET state='failed',updated_at=? WHERE stage=? AND attempts>=5 AND (state='pending' OR (state='running' AND lease_until<=?))",
                (stamp, stage, stamp),
            )
            row = connection.execute(
                "SELECT j.id,j.event_id,j.attempts,e.camera_id,e.person_id,e.global_person_id,e.occurred_at,e.metadata_json FROM object_jobs j JOIN events e ON e.id=j.event_id WHERE j.stage=? AND j.attempts<5 AND ((j.state='pending' AND j.next_attempt_at<=?) OR (j.state='running' AND j.lease_until<=?)) ORDER BY j.id LIMIT 1",
                (stage, stamp, stamp),
            ).fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            connection.execute(
                "UPDATE object_jobs SET state='running',attempts=attempts+1,lease_id=?,lease_until=?,updated_at=? WHERE id=?",
                (lease, format_utc(now + timedelta(minutes=5)), stamp, row["id"]),
            )
            result = dict(row)
            result["object_observation"] = json.loads(result.pop("metadata_json"))[
                "object"
            ]
            result["lease_id"] = lease
            return result

    # 유효한 임대의 결과만 병합하고 식별 단계에서만 인물 연결을 생성한다.
    def complete_object_job(self, stage, job_id, completion: ObjectJobCompletion):
        # 현재 임대 ID와 만료 시간을 함께 검사해 이전 작업자의 늦은 결과가 덮어쓰지 못하게 한다.
        now = utc_now()
        stamp = format_utc(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT j.*,e.camera_id,e.person_id,e.occurred_at,e.metadata_json FROM object_jobs j JOIN events e ON e.id=j.event_id WHERE j.id=? AND j.stage=? AND j.state='running' AND j.lease_id=? AND j.lease_until>?",
                (job_id, stage, completion.lease_id, stamp),
            ).fetchone()
            if row is None:
                return False
            if stage != "identity" and completion.global_person_id is not None:
                raise ValueError("Only identity jobs may assign a global person ID")
            if stage != "identity" and completion.identity_descriptor is not None:
                raise ValueError("Only identity jobs may submit identity features")
            state = completion.outcome
            if state == "retry":
                state = "pending" if row["attempts"] < 5 else "failed"
            metadata = json.loads(row["metadata_json"])
            global_person_id = completion.global_person_id
            result_metadata = dict(completion.metadata)
            if completion.identity_descriptor is not None:
                global_person_id, match = resolve_identity(
                    connection,
                    row,
                    metadata["object"]["tracking_session_id"],
                    completion.identity_descriptor,
                    stamp,
                )
                result_metadata["match"] = match
            # 두 단계가 독립적으로 끝나므로 DB의 최신 metadata에서 자기 단계의 결과만 바꾼다.
            metadata[stage] = {
                "status": state,
                "updated_at": stamp,
                "result": result_metadata,
            }
            if stage == "identity" and global_person_id is not None:
                # person_id는 재접속 후 다시 쓰일 수 있으므로 카메라와 추적 세션을 함께 식별한다.
                session = metadata["object"]["tracking_session_id"]
                existing = connection.execute(
                    "SELECT global_person_id FROM person_identity_links WHERE camera_id=? AND tracking_session_id=? AND person_id=?",
                    (row["camera_id"], session, row["person_id"]),
                ).fetchone()
                if (
                    existing
                    and existing["global_person_id"] != global_person_id
                ):
                    raise ValueError("Identity already assigned for this camera track")
                connection.execute(
                    "INSERT OR IGNORE INTO person_identity_links VALUES (?,?,?,?,?)",
                    (
                        row["camera_id"],
                        session,
                        row["person_id"],
                        global_person_id,
                        stamp,
                    ),
                )
                connection.execute(
                    "UPDATE events SET global_person_id=? WHERE camera_id=? AND person_id=? AND json_extract(metadata_json,'$.tracking_session_id')=?",
                    (
                        global_person_id,
                        row["camera_id"],
                        row["person_id"],
                        session,
                    ),
                )
            connection.execute(
                "UPDATE events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, allow_nan=False), row["event_id"]),
            )
            connection.execute(
                "UPDATE object_jobs SET state=?,next_attempt_at=?,lease_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (
                    state,
                    format_utc(
                        now + timedelta(seconds=min(300, 15 * 2 ** row["attempts"]))
                    ),
                    stamp,
                    job_id,
                ),
            )
            return True

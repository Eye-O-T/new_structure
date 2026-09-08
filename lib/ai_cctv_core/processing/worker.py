"""Data 작업을 가져와 인물 식별·분석 플러그인의 결과를 보고한다."""

import asyncio
from pathlib import Path

from ai_cctv_core.contracts.objects import ObjectJobCompletion, ObjectObservation
from .plugins import ObjectProcessor


def safe_crop(root: Path, relative: str) -> Path:
    # 사람 영역 이미지가 공유 저장소 내부에 있는지 확인한다.
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Crop must exist inside snapshots storage")
    return path


class ObjectWorker:
    def __init__(
        self, stage, client, snapshots_root, plugin: ObjectProcessor, timeout_seconds=120
    ):
        if stage not in {"identity", "analysis"}:
            raise ValueError("Unsupported object processing stage")
        self.stage = stage
        self.client = client
        self.root = snapshots_root
        self.plugin = plugin
        self.ready = False
        self.last_error = None
        self.stalled = False
        self.timeout_seconds = timeout_seconds
        self.last_outcome = None

    async def once(self):
        # claim으로 작업을 임대하고, 완료 시 같은 lease_id로 처리 권한을 증명한다.
        response = await self.client.post(f"/object-jobs/{self.stage}/claim")
        response.raise_for_status()
        self.ready = True
        job = response.json()["job"]
        if job is None:
            return False
        try:
            observation = ObjectObservation.model_validate(job["object_observation"])
            path = safe_crop(self.root, observation.crop_path)
            # 모델은 별도 스레드에서 실행하고 대기 시간을 제한한다.
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.plugin.process, job, path),
                timeout=self.timeout_seconds,
            )
            completion = ObjectJobCompletion.model_validate(
                {**raw, "lease_id": job["lease_id"]}
            )
            if self.stage == "analysis" and completion.global_person_id is not None:
                # 인물 식별과 속성 분석의 책임을 분리해 분석 모델이 ID를 덮어쓰지 못하게 한다.
                raise ValueError("An analyzer cannot assign identity")
        except TimeoutError:
            # 시간 초과 후에도 모델 스레드는 남을 수 있어 추가 작업을 멈춘다.
            self.stalled = True
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="retry",
                metadata={"error_code": "MODEL_TIMEOUT"},
            )
        except (ValueError, TypeError, FileNotFoundError):
            # 잘못된 결과·없는 파일은 재시도하지 않는다.
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="failed",
                metadata={"error_code": "INVALID_OBJECT_RESULT_OR_CROP"},
            )
        except Exception:
            # 나머지 모델 오류는 Data에 재시도를 요청한다.
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="retry",
                metadata={"error_code": "ANALYZER_UNAVAILABLE"},
            )
        response = await self.client.post(
            f"/object-jobs/{self.stage}/{job['id']}/complete",
            json=completion.model_dump(mode="json"),
        )
        response.raise_for_status()
        # HTTP 200도 임대 만료·중복 완료일 수 있다. 수락 여부는 본문으로 확인한다.
        if response.json().get("accepted") is not True:
            self.last_outcome = "rejected"
            self.last_error = "COMPLETION_NOT_ACCEPTED"
            return True
        self.last_outcome = completion.outcome
        self.last_error = (
            None
            if completion.outcome in {"complete", "unconfigured"}
            else completion.metadata.get("error_code")
        )
        return True

    async def run(self):
        while True:
            if self.stalled:
                self.ready = False
                await asyncio.sleep(5)
                continue
            try:
                worked = await self.once()
            except Exception:
                self.ready = False
                self.last_error = "DATA_UNAVAILABLE"
                worked = False
            # 빈 대기열·통신 실패 시 조회 간격을 늘린다.
            await asyncio.sleep(0.05 if worked else 1)

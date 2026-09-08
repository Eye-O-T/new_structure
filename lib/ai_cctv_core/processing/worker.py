"""Durable Data jobs used by preprocessing identity and metadata analysis."""

# Data에 보관된 작업을 하나씩 가져와 모델에 전달하고 결과를 돌려주는 공통 작업자다.
# 작업 보관·재할당은 Data가, 이미지 분석은 서비스별 플러그인이 담당한다.

import asyncio
from pathlib import Path

from ai_cctv_core.contracts.objects import ObjectJobCompletion, ObjectObservation


def safe_crop(root: Path, relative: str) -> Path:
    # crop은 원본 영상에서 사람 영역만 잘라낸 이미지다. 전달받은 경로가
    # 실제 공유 저장소 안의 파일인지 검사한 후 모델에 넘긴다.
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Crop must exist inside snapshots storage")
    return path


class ObjectWorker:
    def __init__(self, stage, client, snapshots_root, plugin, timeout_seconds=120):
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
        # claim은 단순 조회가 아니라 일정 시간 동안 이 작업을 맡겠다는 요청이다.
        # 응답의 lease_id를 완료 보고에 함께 보내 처리 권한을 증명한다.
        response = await self.client.post(f"/object-jobs/{self.stage}/claim")
        response.raise_for_status()
        self.ready = True
        job = response.json()["job"]
        if job is None:
            return False
        try:
            observation = ObjectObservation.model_validate(job["object_observation"])
            path = safe_crop(self.root, observation.crop_path)
            # 동기식 모델 호출은 별도 스레드에서 실행하고, 기다리는 시간에는 상한을 둔다.
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
            # 기다리기를 취소해도 스레드 내부 모델은 계속 돌 수 있다. 추가 모델 호출을
            # 쌓지 않도록 작업자를 멈춤 상태로 두고 이번 작업은 재시도 대상으로 돌려준다.
            self.stalled = True
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="retry",
                metadata={"error_code": "MODEL_TIMEOUT"},
            )
        except (ValueError, TypeError, FileNotFoundError):
            # 형식 오류나 없는 입력 파일은 같은 입력으로 반복해도 해결되지 않아 실패로 남긴다.
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="failed",
                metadata={"error_code": "INVALID_OBJECT_RESULT_OR_CROP"},
            )
        except Exception:
            # 일시적인 모델 장애 등 나머지 오류는 Data가 나중에 재시도할 수 있게 보고한다.
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
            # 작업이 없거나 Data가 응답하지 않을 때는 조회 간격을 늘려 불필요한 부하를 줄인다.
            await asyncio.sleep(0.05 if worked else 1)

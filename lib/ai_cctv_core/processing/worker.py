"""Data 작업을 가져와 인물 식별·분석 플러그인의 결과를 보고한다."""

import asyncio
import math
import time
from pathlib import Path, PureWindowsPath

from ai_cctv_core.contracts.objects import ObjectJobCompletion, ObjectObservation
from .plugins import ObjectProcessor


def safe_crop(root: Path, relative: str) -> Path:
    # 사람 영역 이미지가 공유 저장소 내부에 있는지 확인한다.
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or PureWindowsPath(relative).drive
        or "\\" in relative
        or "\x00" in relative
    ):
        raise ValueError("Crop must use a relative storage path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Crop must exist inside snapshots storage")
    return path


# Data에서 작업 하나씩 임대해 모델을 실행하고 같은 임대 ID로 결과를 제출한다.
class ObjectWorker:
    def __init__(
        self,
        stage,
        client,
        snapshots_root,
        plugin: ObjectProcessor,
        timeout_seconds=120,
    ):
        if stage not in {"identity", "analysis"}:
            raise ValueError("Unsupported object processing stage")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds < 280:
            raise ValueError("Model timeout must fit inside the five-minute lease")
        self.stage = stage
        self.client = client
        self.root = snapshots_root
        self.plugin = plugin
        self.ready = False
        self.last_error = None
        self.stalled = False
        self.timeout_seconds = timeout_seconds
        self.last_outcome = None
        self._pending = None
        self._pending_until = 0.0

    async def _report(self):
        # 응답 유실 시 같은 임대와 본문을 다시 보낸다. 새 작업을 미리 가져오지 않는다.
        if time.monotonic() >= self._pending_until:
            self._pending = None
            self.last_error = "COMPLETION_LEASE_EXPIRED"
            self.last_outcome = "rejected"
            return True
        job_id, completion = self._pending
        payload = completion.model_dump(mode="json")
        # 선택 기능을 쓰지 않는 교체 플러그인은 기존 완료 JSON 형식을 유지한다.
        if payload.get("identity_descriptor") is None:
            payload.pop("identity_descriptor", None)
        response = await self.client.post(
            f"/object-jobs/{self.stage}/{job_id}/complete", json=payload
        )
        if response.status_code in {400, 404, 409, 422}:
            self._pending = None
            self.last_outcome = "failed"
            self.last_error = "COMPLETION_INVALID"
            return True
        response.raise_for_status()
        accepted = response.json().get("accepted")
        if not isinstance(accepted, bool):
            raise ValueError("Invalid completion acknowledgement")
        self.ready = True
        self._pending = None
        if not accepted:
            self.last_outcome = "rejected"
            self.last_error = "COMPLETION_NOT_ACCEPTED"
            return True
        self.last_outcome = completion.outcome
        self.last_error = (
            "PROCESSOR_UNCONFIGURED"
            if completion.outcome == "unconfigured"
            else None
            if completion.outcome == "complete"
            else completion.metadata.get("error_code", "MODEL_FAILED")
        )
        return True

    async def once(self):
        if self._pending is not None:
            return await self._report()
        if self.stalled:
            self.ready = False
            return False
        # claim으로 작업을 임대하고, 완료 시 같은 lease_id로 처리 권한을 증명한다.
        response = await self.client.post(f"/object-jobs/{self.stage}/claim")
        response.raise_for_status()
        self.ready = True
        if self.last_error == "DATA_UNAVAILABLE":
            self.last_error = None
        job = response.json()["job"]
        if job is None:
            # 응답 유실 뒤 중복 완료가 거부될 수도 있다. 마지막 결과는 보존하되,
            # 정상 빈 조회로 확인한 작업 통로에 이 진단을 영구 장애로 남기지 않는다.
            if self.last_error == "COMPLETION_NOT_ACCEPTED":
                self.last_error = None
            return False
        claimed_at = time.monotonic()
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
            if self.stage == "analysis" and (
                completion.global_person_id is not None
                or getattr(completion, "identity_descriptor", None) is not None
            ):
                # 인물 식별과 속성 분석의 책임을 분리해 분석 모델이 ID를 덮어쓰지 못하게 한다.
                raise ValueError("An analyzer cannot assign identity")
        except TimeoutError:
            # 격리 자식을 종료한 실행기만 다시 사용한다. 직접 주입한 스레드 모델은 멈춘다.
            self.stalled = not getattr(self.plugin, "restartable", False)
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
            self.stalled = not getattr(self.plugin, "restartable", True)
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="retry",
                metadata={"error_code": "ANALYZER_UNAVAILABLE"},
            )
        self._pending = (job["id"], completion)
        self._pending_until = claimed_at + 290
        return await self._report()

    # 대기열이 비었거나 통신이 실패하면 쉬었다가 재조회하며 준비 상태도 함께 갱신한다.
    async def run(self):
        while True:
            if self.stalled and self._pending is None:
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

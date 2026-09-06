"""Durable Data jobs used by preprocessing identity and metadata analysis."""

import asyncio
from pathlib import Path

from ai_cctv_core.contracts.objects import ObjectJobCompletion, ObjectObservation


def safe_crop(root: Path, relative: str) -> Path:
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
        response = await self.client.post(f"/object-jobs/{self.stage}/claim")
        response.raise_for_status()
        self.ready = True
        job = response.json()["job"]
        if job is None:
            return False
        try:
            observation = ObjectObservation.model_validate(job["object_observation"])
            path = safe_crop(self.root, observation.crop_path)
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.plugin.process, job, path),
                timeout=self.timeout_seconds,
            )
            completion = ObjectJobCompletion.model_validate(
                {**raw, "lease_id": job["lease_id"]}
            )
            if self.stage == "analysis" and completion.global_person_id is not None:
                raise ValueError("An analyzer cannot assign identity")
        except TimeoutError:
            # Do not spawn further model calls while an uninterruptible call runs.
            self.stalled = True
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="retry",
                metadata={"error_code": "MODEL_TIMEOUT"},
            )
        except (ValueError, TypeError, FileNotFoundError):
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="failed",
                metadata={"error_code": "INVALID_OBJECT_RESULT_OR_CROP"},
            )
        except Exception:
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
            await asyncio.sleep(0.05 if worked else 1)

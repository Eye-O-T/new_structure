"""Replace this factory with the independently developed person Re-ID backend."""

from pathlib import Path


class IdentityBlackBox:
    def process(self, job: dict, crop_path: Path) -> dict:
        return {
            "outcome": "unconfigured",
            "metadata": {"reason": "identity_backend_not_implemented"},
        }

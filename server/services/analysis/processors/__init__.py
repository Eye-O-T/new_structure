"""Replace this factory with the independently developed metadata analyzer."""

from pathlib import Path


class MetadataBlackBox:
    def process(self, job: dict, crop_path: Path) -> dict:
        # Another developer owns attribute extraction and model selection.
        return {
            "outcome": "unconfigured",
            "metadata": {"reason": "metadata_backend_not_implemented"},
        }

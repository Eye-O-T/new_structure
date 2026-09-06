"""Version 1 in-process BGR frame input and per-frame person detection output."""

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_cctv_core.contracts.objects import LiveObject
from ai_cctv_core.identifiers import validate_camera_id


class DetectionFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    camera_id: str
    tracking_session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    observed_at: datetime
    image: Any = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def valid_frame(self):
        validate_camera_id(self.camera_id)
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        shape = getattr(self.image, "shape", ())
        if (
            len(shape) != 3
            or shape[2] != 3
            or not all(0 < n <= 16384 for n in shape[:2])
        ):
            raise ValueError("image must be an H x W x 3 BGR frame")
        if str(getattr(self.image, "dtype", "")) != "uint8":
            raise ValueError("image must contain uint8 BGR pixels")
        return self


class DetectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    objects: list[LiveObject] = Field(default_factory=list, max_length=100)


class DetectionProcessor(Protocol):
    def reset(self) -> None:
        """Discard tracking state when the camera stream starts a new session."""
        ...

    def process(self, frame: DetectionFrame) -> DetectionResult:
        """Return person IDs, pixel xyxy boxes and confidence; do not mutate pixels."""
        ...

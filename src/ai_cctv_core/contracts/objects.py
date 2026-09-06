"""Versioned contracts between detection, identity and attribute analysis."""

from typing import Any, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObjectObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    tracking_session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    bbox: tuple[int, int, int, int]
    frame_width: int = Field(gt=0, le=16384)
    frame_height: int = Field(gt=0, le=16384)
    crop_path: str = Field(min_length=1, max_length=4096)
    annotated_snapshot_path: str | None = Field(default=None, max_length=4096)
    object_class: Literal["person"] = "person"

    @model_validator(mode="after")
    def valid_box(self):
        x1, y1, x2, y2 = self.bbox
        if not (0 <= x1 < x2 <= self.frame_width and 0 <= y1 < y2 <= self.frame_height):
            raise ValueError("bbox must be a nonempty rectangle inside the frame")
        return self


class ObjectJobCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    outcome: Literal["complete", "retry", "failed", "unconfigured"]
    global_person_id: str | None = Field(default=None, min_length=1, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_result(self):
        import json

        if len(json.dumps(self.metadata, allow_nan=False).encode()) > 65536:
            raise ValueError("analysis metadata exceeds 64 KiB")
        if self.global_person_id is not None and self.outcome != "complete":
            raise ValueError("only a completed identity result can assign a global ID")
        return self


class LiveObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person_id: str = Field(min_length=1, max_length=256)
    bbox: tuple[int, int, int, int]
    confidence: float = Field(ge=0, le=1)


class LiveObjects(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracking_session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    observed_at: datetime
    frame_width: int = Field(gt=0, le=16384)
    frame_height: int = Field(gt=0, le=16384)
    objects: list[LiveObject] = Field(max_length=100)

    @model_validator(mode="after")
    def valid_frame(self):
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if len({obj.person_id for obj in self.objects}) != len(self.objects):
            raise ValueError("person IDs must be unique within a frame")
        for obj in self.objects:
            x1, y1, x2, y2 = obj.bbox
            if not (
                0 <= x1 < x2 <= self.frame_width and 0 <= y1 < y2 <= self.frame_height
            ):
                raise ValueError("bbox is outside frame bounds")
        return self

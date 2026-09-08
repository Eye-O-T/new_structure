"""Versioned contracts between detection, identity and attribute analysis."""

# 계약(contract)은 서비스들이 주고받기로 약속한 데이터 형식과 검증 규칙이다.
# 영상 파일 자체 대신 좌표와 공유 저장소의 상대 경로를 전달한다.

from typing import Any, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObjectObservation(BaseModel):
    # 한 번 관측한 사람의 분석 입력이다. 재접속마다 세션 ID를 바꾸므로,
    # 같은 카메라에서 재사용된 person_id를 이전 추적 결과와 구별할 수 있다.
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
        # bbox는 픽셀 단위의 (왼쪽, 위, 오른쪽, 아래) 좌표다. 잘라낼 영역이
        # 영상 내부에 있고 넓이·높이가 모두 양수인지 검사한다.
        x1, y1, x2, y2 = self.bbox
        if not (0 <= x1 < x2 <= self.frame_width and 0 <= y1 < y2 <= self.frame_height):
            raise ValueError("bbox must be a nonempty rectangle inside the frame")
        return self


class ObjectJobCompletion(BaseModel):
    # lease_id는 작업을 가져갈 때 받은 임시 처리 권한의 번호다. Data가 이를 확인해
    # 만료되거나 다른 작업자에게 넘어간 작업의 오래된 결과를 구별한다.
    model_config = ConfigDict(extra="forbid")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    outcome: Literal["complete", "retry", "failed", "unconfigured"]
    global_person_id: str | None = Field(default=None, min_length=1, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_result(self):
        import json

        # 추가 분석 결과의 크기를 제한하고, 재시도·실패 결과가 인물 ID를 확정하지 못하게 한다.
        if len(json.dumps(self.metadata, allow_nan=False).encode()) > 65536:
            raise ValueError("analysis metadata exceeds 64 KiB")
        if self.global_person_id is not None and self.outcome != "complete":
            raise ValueError("only a completed identity result can assign a global ID")
        return self


class LiveObject(BaseModel):
    # person_id는 카메라와 추적 세션 안에서만 유효하다. 여러 카메라의 동일 인물을
    # 묶는 global_person_id는 identity 단계에서 별도로 결정한다.
    model_config = ConfigDict(extra="forbid")
    person_id: str = Field(min_length=1, max_length=256)
    bbox: tuple[int, int, int, int]
    confidence: float = Field(ge=0, le=1)


class LiveObjects(BaseModel):
    # 모바일이 현재 박스를 그릴 수 있도록 좌표뿐 아니라 원본 영상 크기도 함께 보낸다.
    # observed_at은 수신자가 오래된 좌표를 구별할 수 있도록 관측 시각을 담는다.
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

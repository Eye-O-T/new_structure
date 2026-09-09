# 탐지 모델을 바꾸어도 카메라 처리 코드가 유지되도록 입력과 출력의 모양을 정한다.
# 이 계약은 같은 프로세스 안에서 이미지를 전달하며, HTTP로 영상 배열을 보내는 형식은 아니다.

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_cctv_core.contracts.objects import LiveObject
from ai_cctv_core.identifiers import validate_camera_id


# 하나의 추적 세션에서 관측한 BGR 영상과 시간·카메라 식별자를 플러그인에 전달한다.
class DetectionFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    camera_id: str
    tracking_session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    observed_at: datetime
    # OpenCV의 기본 영상은 높이 × 너비 × 3 배열이며 색 순서는 파랑·초록·빨강(BGR)이다.
    # 큰 영상 배열은 JSON 직렬화와 객체 출력에서 제외한다.
    image: Any = Field(exclude=True, repr=False)

    # 카메라 ID·시간대와 OpenCV가 기대하는 영상 모양·uint8 자료형을 검사한다.
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


# 탐지 플러그인의 결과를 ID·좌표·신뢰도가 검증된 최대 100개 객체로 제한한다.
class DetectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    objects: list[LiveObject] = Field(default_factory=list, max_length=100)


class DetectionProcessor(Protocol):
    # Protocol은 구현 클래스가 갖춰야 할 함수 모양을 나타낸다.
    # reset은 영상 재접속 때 이전 추적 상태를 버리고, process는 한 프레임을 처리한다.
    def reset(self) -> None:
        """새 영상 세션에서 이전 추적 상태를 버린다."""
        ...

    def process(self, frame: DetectionFrame) -> DetectionResult:
        """인물 ID·픽셀 xyxy 박스·신뢰도를 반환하고 입력 영상은 변경하지 않는다."""
        ...

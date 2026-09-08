"""Replace this factory with the independently developed metadata analyzer."""

# 다른 개발자가 작성할 YOLO·CNN 등의 속성 분석 모델을 연결하는 자리다.
# CNN은 이미지에서 특징을 추출하는 신경망의 한 종류이며 구체적인 모델 선택은 여기서 확정하지 않는다.
# job의 관측 정보와 crop_path의 사람 이미지를 받아 분석 결과를 metadata에 담는 계약이다.

from pathlib import Path


class MetadataBlackBox:
    def process(self, job: dict, crop_path: Path) -> dict:
        # 현재는 빈 연결점이므로 성공이나 가짜 속성을 반환하지 않고 미구현 상태를 명시한다.
        return {
            "outcome": "unconfigured",
            "metadata": {"reason": "metadata_backend_not_implemented"},
        }

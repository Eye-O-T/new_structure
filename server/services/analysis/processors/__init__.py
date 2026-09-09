# 기본 CPU 분석기와 기존 배포의 미설정 플러그인을 같은 팩토리 경로에서 제공한다.
# job의 관측 정보와 crop_path의 사람 이미지를 받아 분석 결과를 metadata에 담는다.

from pathlib import Path

from .appearance import LocalAppearanceAnalyzer


__all__ = ["LocalAppearanceAnalyzer", "MetadataBlackBox"]


# 분석 플러그인의 교체 지점이며 현재는 인물 ID나 분석 속성을 확정하지 않는다.
class MetadataBlackBox:
    def process(self, job: dict, crop_path: Path) -> dict:
        # 현재는 빈 연결점이므로 성공이나 가짜 속성을 반환하지 않고 미구현 상태를 명시한다.
        return {
            "outcome": "unconfigured",
            "metadata": {"reason": "metadata_backend_not_implemented"},
        }

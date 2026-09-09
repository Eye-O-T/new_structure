# 기본 OSNet, 명시적으로 선택하는 이전 외관 특징 추출기와 미설정 플러그인을 제공한다.
# 특징 비교·지속적인 global_person_id 연결은 Data가 맡고 여기서는 이미지만 읽는다.

from pathlib import Path

from .appearance import LocalAppearanceIdentity
from .osnet import OsNetIdentity


__all__ = ["IdentityBlackBox", "LocalAppearanceIdentity", "OsNetIdentity"]


# 식별 모델 연결 전 작업 프로토콜을 검증하려고 명시적으로 선택하는 미설정 구현이다.
class IdentityBlackBox:
    def process(self, job: dict, crop_path: Path) -> dict:
        # job에는 관측 정보가, crop_path에는 검증된 사람 이미지 경로가 전달된다.
        # 실제 구현도 이 함수 모양과 ObjectJobCompletion의 결과 규칙을 지켜 교체한다.
        return {
            "outcome": "unconfigured",
            "metadata": {"reason": "identity_backend_not_implemented"},
        }

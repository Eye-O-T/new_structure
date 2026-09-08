# 다른 개발자가 구현할 인물 재식별(Re-ID)의 연결 지점이다.
# 재식별은 여러 카메라에서 관측된 사람이 같은 사람인지 판단해 global_person_id를 정한다.
# 현재는 미구현 상태만 반환한다. 임의 ID를 발급해 식별이 완료된 것처럼 보이게 하지 않는다.

from pathlib import Path


class IdentityBlackBox:
    def process(self, job: dict, crop_path: Path) -> dict:
        # job에는 관측 정보가, crop_path에는 검증된 사람 이미지 경로가 전달된다.
        # 실제 구현도 이 함수 모양과 ObjectJobCompletion의 결과 규칙을 지켜 교체한다.
        return {
            "outcome": "unconfigured",
            "metadata": {"reason": "identity_backend_not_implemented"},
        }

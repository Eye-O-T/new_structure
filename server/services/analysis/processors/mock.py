import time
from pathlib import Path


class MockRiskAnalyzer:
    """팀 파이프라인 연결 확인용 더미 위험도 분석기."""

    def __init__(self):
        print("[System] Mock risk analyzer loaded.")

    def process(self, job: dict, crop_path: Path) -> dict:
        print(f"[AI] Mock crop analysis: {crop_path}")
        time.sleep(0.5)

        return {
            "outcome": "complete",
            "metadata": {
                "backend": "mock_risk_analyzer",
                "schema_version": 1,
                "is_mock": True,
                "name": "Unknown",
                "age_group": "20s",
                "weapon_detected": True,
                "weapon_type": "knife",
                "risk_level": "HIGH",
                "status_message": "흉기 소지 의심",
            },
        }
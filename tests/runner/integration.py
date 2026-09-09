"""격리된 Data·External 프로세스 사이의 실제 HTTP 계약을 확인한다."""

from datetime import datetime, timezone
import os
from uuid import uuid4

import httpx

from server.services.external.app.security.passwords import hash_password


# 격리된 실행 서버에서 내부 이벤트 생성부터 분석 결과 공개 조회·로그아웃까지 왕복 검증한다.
def verify(data_url: str, external_url: str) -> None:
    # 매 실행의 사용자·카메라·추적 세션을 구분해 이전 검증 데이터와 충돌하지 않게 한다.
    suffix = uuid4().hex
    password = "integration-test-password-123"
    admin_token = "test-external-token-0000000000000000000000"
    inference_token = "test-inference-token-000000000000000000000"
    analysis_token = "test-analysis-token-0000000000000000000000"
    with httpx.Client(base_url=data_url, trust_env=False, timeout=10) as data:
        assert data.get("/cameras/enabled").status_code == 401
        data.headers["X-Internal-Token"] = admin_token
        user = data.post(
            "/users",
            json={
                "username": f"test-{suffix}",
                "password_hash": hash_password(password),
                "role": "admin",
                "is_active": True,
            },
        )
        user.raise_for_status()
        camera_id = f"test-{suffix}"
        camera = data.post(
            "/cameras",
            json={
                "camera_id": camera_id,
                "name": "HTTP integration",
                "stream_path": camera_id,
                "enabled": False,
            },
        )
        camera.raise_for_status()
        data.headers["X-Internal-Token"] = inference_token
        event = data.post(
            "/events",
            json={
                "camera_id": camera_id,
                "event_type": "person_appeared",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "person_id": "1",
                "object_observation": {
                    "tracking_session_id": suffix,
                    "bbox": [0, 0, 20, 40],
                    "frame_width": 100,
                    "frame_height": 100,
                    "crop_path": f"{camera_id}/test.jpg",
                },
            },
        )
        event.raise_for_status()
        event_id = event.json()["id"]
        # 같은 이벤트를 분석 전용 권한으로 임대·완료하여 서비스별 권한 경계를 함께 확인한다.
        data.headers["X-Internal-Token"] = analysis_token
        claimed = data.post("/object-jobs/analysis/claim")
        claimed.raise_for_status()
        job = claimed.json()["job"]
        assert job is not None and job["event_id"] == event_id
        completion = {
            "lease_id": job["lease_id"],
            "outcome": "complete",
            "metadata": {"test": "http-round-trip"},
        }
        completed = data.post(
            f"/object-jobs/analysis/{job['id']}/complete", json=completion
        )
        completed.raise_for_status()
        assert completed.json() == {"accepted": True}
        # 완료 요청 재전송이 동일 작업을 두 번 확정하지 않는지 실제 HTTP 응답으로 확인한다.
        duplicate = data.post(
            f"/object-jobs/analysis/{job['id']}/complete", json=completion
        )
        duplicate.raise_for_status()
        assert duplicate.json() == {"accepted": False}

    with httpx.Client(base_url=external_url, trust_env=False, timeout=10) as external:
        assert external.get(f"/events/{event_id}").status_code == 401
        login = external.post(
            "/auth/login", json={"username": f"test-{suffix}", "password": password}
        )
        login.raise_for_status()
        tokens = login.json()
        external.headers["Authorization"] = f"Bearer {tokens['access_token']}"
        result = external.get(f"/events/{event_id}")
        result.raise_for_status()
        assert result.json()["metadata"]["analysis"]["result"] == {
            "test": "http-round-trip"
        }
        logout = external.post(
            "/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )
        assert logout.status_code == 204
        assert external.get(f"/events/{event_id}").status_code == 401


if __name__ == "__main__":
    verify(os.environ["TEST_DATA_URL"], os.environ["TEST_EXTERNAL_URL"])
    print("HTTP integration passed: authentication, events, analysis jobs, logout")

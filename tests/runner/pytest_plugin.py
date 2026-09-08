"""개발 서비스에서 pytest를 실행해도 실제 접속 정보는 테스트에 상속하지 않는다."""

import os


def pytest_load_initial_conftests(early_config, parser, args):
    # 모듈 수집 전에 지워야 모듈 전역의 create_app()도 배포 설정을 읽지 않는다.
    # 실행 중인 서버가 아니라 별도 pytest 프로세스의 환경만 바뀐다.
    prefixes = (
        "AI_CCTV_",
        "DATA_",
        "INTERNAL_",
        "INFERENCE_",
        "MEDIA_",
        "MEDIAMTX_",
        "JWT_",
        "ACCESS_",
        "REFRESH_",
        "COOKIE_",
        "PUBLIC_",
        "PUSH_",
        "FIREBASE_",
        "EDGE_",
        "RECOVERY_",
        "CENTRAL_",
        "MODEL_",
        "SNAPSHOTS_",
        "RECORDINGS_",
        "ANALYSIS_",
        "DETECTION_",
        "IDENTITY_",
        "CAMERA_",
        "DISAPPEAR_",
        "EVENT_",
        "BATTERY_",
        "STORAGE_",
    )
    for name in list(os.environ):
        if name.startswith(prefixes):
            del os.environ[name]

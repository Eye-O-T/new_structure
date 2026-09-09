"""파일 기반 검증은 실행 위치와 관계없이 저장소 루트를 기준으로 한다."""

from pathlib import Path

import pytest


# 패키징 입력과 서버 설정의 상대 경로 검사를 저장소 루트에서 일관되게 실행한다.
@pytest.fixture(autouse=True)
def repository_directory(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[4])

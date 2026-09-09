"""파일 기반 검증은 실행 위치와 관계없이 저장소 루트를 기준으로 한다."""

from pathlib import Path

import pytest


# 상대 경로로 저장소 파일을 읽는 검사가 pytest 실행 위치의 영향을 받지 않게 한다.
@pytest.fixture(autouse=True)
def repository_directory(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[3])

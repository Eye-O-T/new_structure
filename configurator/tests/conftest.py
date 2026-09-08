"""파일 기반 검증은 실행 위치와 관계없이 저장소 루트를 기준으로 한다."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def repository_directory(monkeypatch):
    monkeypatch.chdir(Path(__file__).resolve().parents[2])

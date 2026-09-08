"""Load explicitly configured Python factories without importing model SDKs eagerly."""

# 플러그인은 정해진 함수 모양을 지키면서 교체할 수 있는 구현체다.
# 공통 작업 처리는 유지하고, 팀원이 개발한 인물 식별·추가 분석 모델만 연결한다.

from importlib import import_module
from pathlib import Path
from typing import Protocol


class ObjectProcessor(Protocol):
    def process(self, job: dict, crop_path: Path) -> dict:
        """Return an ObjectJobCompletion mapping without its transport lease_id."""
        ...


def load_factory(reference: str):
    # '모듈경로:생성함수'를 읽어 필요한 구현만 불러온다. 이 단계 전에는
    # 해당 모델의 무거운 라이브러리를 공통 모듈이 직접 불러오지 않는다.
    module_name, separator, factory_name = reference.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("Plugin must be an importable module:factory reference")
    factory = getattr(import_module(module_name), factory_name)
    if not callable(factory):
        raise ValueError("Plugin factory must be callable")
    return factory

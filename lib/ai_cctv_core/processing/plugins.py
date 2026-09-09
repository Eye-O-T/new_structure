"""지정한 팩토리로 인물 식별·분석 구현체를 불러온다."""

from importlib import import_module
from pathlib import Path
from typing import Protocol


# 식별·분석 플러그인은 작업 메타데이터와 사람 영역 파일을 받아 완료 결과를 돌려준다.
class ObjectProcessor(Protocol):
    def process(self, job: dict, crop_path: Path) -> dict:
        """lease_id를 제외한 ObjectJobCompletion 필드를 반환한다."""
        ...


# 설정의 module:factory 참조를 해석해 호출 가능한 플러그인 생성 함수를 찾는다.
def load_factory(reference: str):
    # 모듈경로:생성함수 형식이며, 선택한 모델 라이브러리만 불러온다.
    module_name, separator, factory_name = reference.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("Plugin must be an importable module:factory reference")
    factory = getattr(import_module(module_name), factory_name)
    if not callable(factory):
        raise ValueError("Plugin factory must be callable")
    return factory

"""호환성 별칭 모듈: OpenCV 색상 분석기는 제거되었으며 PAR 속성 분석기만 사용한다."""

from .attributes import PersonAttributeAnalyzer

LocalAppearanceAnalyzer = PersonAttributeAnalyzer

__all__ = ["LocalAppearanceAnalyzer", "PersonAttributeAnalyzer"]

# 객체 관측을 받아 추가 메타데이터만 계산하는 분석기 모음이다.
# 기본 분석기는 학습된 PAR 번들(/models/par_serving_exp3.pt)을 사용하는 PersonAttributeAnalyzer다.

from .attributes import PersonAttributeAnalyzer

LocalAppearanceAnalyzer = PersonAttributeAnalyzer


__all__ = ["PersonAttributeAnalyzer", "LocalAppearanceAnalyzer"]

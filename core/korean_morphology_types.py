"""형태소 분석 결과 타입.

korean_morphology.py 와 분리한 이유:
  그 모듈은 kiwipiepy 를 임포트한다. 타입만 필요한 곳(quality/__init__ 의
  재수출 등)까지 형태소 분석기를 끌고 오면, kiwi 미설치 환경에서 타입 힌트
  하나 때문에 임포트가 깨진다.

모든 dataclass 는 to_dict() 를 제공한다 — quality_models 와 같은 규약이다.
Notion 속성이나 metrics.jsonl 에 그대로 직렬화하기 위함이다.
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KeywordStat:
    """본문에서 반복된 내용어 하나."""

    word: str
    count: int
    density: float  # 전체 토큰 대비 비율 (0.0~1.0)

    def to_dict(self) -> dict[str, Any]:
        return {"word": self.word, "count": self.count, "density": self.density}


@dataclass
class StyleStat:
    """문말 어미로 판정한 문체.

    이 블로그는 해요체로 통일한다(가이드). 하나의 글 안에서 해요체와 합니다체가
    섞이면 읽는 사람이 화자가 바뀐 것처럼 느낀다. consistency 는 지배 문체가
    전체 문장에서 차지하는 비율이고, quality_agent 가 임계값과 비교한다.
    """

    dominant: str = "unknown"          # "해요체" | "합니다체" | "해라체" | "unknown"
    consistency: float = 0.0           # 지배 문체 비율 (0.0~1.0)
    distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dominant": self.dominant,
            "consistency": self.consistency,
            "distribution": dict(self.distribution),
        }


@dataclass
class MorphologyStats:
    """글 한 편의 형태소 요약."""

    total_tokens: int = 0
    unique_tokens: int = 0
    noun_count: int = 0
    sentence_count: int = 0
    avg_sentence_length: float = 0.0   # 문장당 평균 글자 수
    lexical_diversity: float = 0.0     # unique / total (TTR)
    top_keywords: list[KeywordStat] = field(default_factory=list)
    style: StyleStat = field(default_factory=StyleStat)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "unique_tokens": self.unique_tokens,
            "noun_count": self.noun_count,
            "sentence_count": self.sentence_count,
            "avg_sentence_length": self.avg_sentence_length,
            "lexical_diversity": self.lexical_diversity,
            "top_keywords": [k.to_dict() for k in self.top_keywords],
            "style": self.style.to_dict(),
        }

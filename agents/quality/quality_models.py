"""품질 검사 결과 데이터 모델.

모든 dataclass는 to_dict()를 제공한다. Notion 페이지 첨부나 로그 저장 시
json.dumps(report.to_dict(), ensure_ascii=False)로 바로 직렬화하기 위함이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Polarity(str, Enum):
    """문장 단위 감정 극성."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class Severity(str, Enum):
    """금칙어 카테고리 심각도.

    BLOCK : 하나라도 걸리면 발행 차단(passed=False)
    WARN  : 사람이 판단할 참고 경고. 발행은 막지 않는다.
    """

    BLOCK = "block"
    WARN = "warn"


@dataclass
class BannedHit:
    """금칙어 적발 1건."""

    word: str  # 사전에 등록된 원형
    matched: str  # 본문에서 실제로 매칭된 표면형
    category: str  # 카테고리 키 (profanity, ad_law, ...)
    category_label: str  # 사람이 읽는 카테고리 이름
    severity: Severity
    start: int  # 원문 기준 시작 오프셋
    end: int  # 원문 기준 종료 오프셋(exclusive)
    context: str  # 앞뒤 문맥 스니펫
    match_type: str  # surface | lemma | pattern

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class SentenceSentiment:
    """문장 1개의 감정 판정 결과."""

    index: int
    text: str
    score: float
    polarity: Polarity
    matched_words: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["polarity"] = self.polarity.value
        return d


@dataclass
class SentimentResult:
    """문서 전체 감정 분석 결과.

    두 가지 척도를 함께 담는다.

    3분류 (positive/neutral/negative_ratio)
        전체 문장 대비 비율. 세 값의 합이 1이다.
        중립 문장이 몇 건인지 드러나므로 문서의 성격 파악에 쓴다.

    이진 (binary_positive_ratio)
        중립을 제외하고 긍정 : 부정만 놓고 본 비율.
        일부 블로그 분석 도구가 쓰는 척도라 수치 비교용으로 제공한다.
        감정 문장이 적으면 크게 흔들리므로 참고값으로만 볼 것.
    """

    positive_ratio: float
    negative_ratio: float
    neutral_ratio: float
    total_sentences: int
    average_score: float
    sentences: list[SentenceSentiment] = field(default_factory=list)

    @property
    def positive_count(self) -> int:
        return sum(1 for s in self.sentences if s.polarity is Polarity.POSITIVE)

    @property
    def negative_count(self) -> int:
        return sum(1 for s in self.sentences if s.polarity is Polarity.NEGATIVE)

    @property
    def polar_count(self) -> int:
        """중립을 제외한 감정 문장 수. 이진 비율의 분모다."""
        return self.positive_count + self.negative_count

    @property
    def binary_positive_ratio(self) -> float | None:
        """긍정 ÷ (긍정 + 부정).

        감정 문장이 하나도 없으면 None을 반환한다. 0.0으로 두면
        '전부 부정'과 구분되지 않기 때문이다.
        """
        total = self.polar_count
        if total == 0:
            return None
        return round(self.positive_count / total, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "positive_ratio": self.positive_ratio,
            "negative_ratio": self.negative_ratio,
            "neutral_ratio": self.neutral_ratio,
            "total_sentences": self.total_sentences,
            "average_score": self.average_score,
            "binary_positive_ratio": self.binary_positive_ratio,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "polar_count": self.polar_count,
            "sentences": [s.to_dict() for s in self.sentences],
        }


@dataclass
class QualityReport:
    """품질 검사 최종 리포트."""

    passed: bool
    banned_words: list[BannedHit]
    sentiment: SentimentResult
    morphology: MorphologyStats
    warnings: list[str] = field(default_factory=list)
    char_count: int = 0
    excluded_lines: list = field(default_factory=list)  # list[ExcludedLine]
    seo: Any = None  # SeoReport | None

    @property
    def blocking_hits(self) -> list[BannedHit]:
        return [h for h in self.banned_words if h.severity is Severity.BLOCK]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "char_count": self.char_count,
            "banned_words": [h.to_dict() for h in self.banned_words],
            "sentiment": self.sentiment.to_dict(),
            "morphology": self.morphology.to_dict(),
            "warnings": self.warnings,
            "excluded_lines": [e.to_dict() for e in self.excluded_lines],
            "seo": self.seo.to_dict() if self.seo else None,
        }
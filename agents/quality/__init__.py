"""블로그 초안 품질 검사 에이전트.

LLM을 사용하지 않는 결정론적 검증이다. 같은 입력이면 항상 같은 출력이
나와야 발행 게이트로 신뢰할 수 있다.

    quality_agent.py        오케스트레이터. 형태소 분석을 한 번만 돌려 공유
    seo_checker.py          제목·본문 길이, 챕터, 줄바꿈, 헤딩·불릿 구조
    sentiment_analyzer.py   KNU 감성사전 기반 문장 극성
    context_rules.py        도메인 문맥 극성 보정 (격파·제압을 부정으로 안 봄)
    banned_rules.py         금칙어 3단계 매칭
    boilerplate_filter.py   캡션·출처·저작권 문구를 분석에서 제외
    quality_models.py       결과 데이터클래스
    quality_gate.py         파이프라인 측정 어댑터 (발행을 막지 않음)

임계값은 config/quality_config.py 에 있다. 형태소 분석기는
core/korean_morphology.py 에 있다 (초안 검색기도 함께 쓰기 때문).

기본 사용법:

    from agents.quality import QualityChecker, format_report

    checker = QualityChecker()
    report = checker.check(draft_markdown)
    if not report.passed:
        for hit in report.blocking_hits:
            print(hit.matched, hit.category_label)
    print(format_report(report))
"""

from config.quality_config import CheckConfig, Format, SeoConfig
from core.korean_morphology_types import KeywordStat, MorphologyStats, StyleStat

from .banned_rules import BannedCategory, BannedWordChecker
from .boilerplate_filter import BoilerplateFilter, ExcludedLine
from .context_rules import ContextualPolarityResolver
from .quality_agent import QualityChecker, format_report
from .quality_models import (
    BannedHit,
    Polarity,
    QualityReport,
    SentenceSentiment,
    SentimentResult,
    Severity,
)
from .sentiment_analyzer import SentimentDictionary
from .seo_checker import SeoChecker, SeoReport

__all__ = [
    "QualityChecker",
    "CheckConfig",
    "format_report",
    "QualityReport",
    "BannedHit",
    "BannedCategory",
    "BannedWordChecker",
    "BoilerplateFilter",
    "ExcludedLine",
    "SentimentDictionary",
    "Format",
    "SeoChecker",
    "SeoConfig",
    "SeoReport",
    "ContextualPolarityResolver",
    "SentimentResult",
    "SentenceSentiment",
    "MorphologyStats",
    "KeywordStat",
    "StyleStat",
    "Polarity",
    "Severity",
]

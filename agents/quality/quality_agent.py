"""품질 검사 오케스트레이터.

형태소 분석 결과를 한 번만 계산해서 감정 분석·금칙어 검사가 공유한다.
LLM 호출은 전혀 없다. 같은 입력이면 항상 같은 출력이 나와야
발행 게이트로 신뢰할 수 있기 때문이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.quality_config import CheckConfig

from core import korean_morphology as morphology
from agents.quality.banned_rules import BannedWordChecker
from agents.quality.boilerplate_filter import BoilerplateFilter
from .context_rules import ContextualPolarityResolver
from agents.quality.quality_models import QualityReport, Severity
from agents.quality.sentiment_analyzer import SentimentDictionary, analyze_sentiment
from config.quality_config import Format, SeoConfig
from agents.quality.seo_checker import SeoChecker, SeoReport
from core.text_normalizer import strip_markdown


class QualityChecker:
    """블로그 초안 품질 검사기.

    사전 로딩과 Kiwi 초기화 비용이 있으므로 인스턴스를 재사용한다.

        checker = QualityChecker()
        report = checker.check(draft_markdown)
        if not report.passed:
            ...
    """

    def __init__(
        self,
        config: CheckConfig | None = None,
        dict_dir: Path | None = None,
        seo_config: SeoConfig | None = None,
    ) -> None:
        self.config = config or CheckConfig()
        self.banned = BannedWordChecker.load(dict_dir)
        self.sentiment_dict = SentimentDictionary.load(dict_dir)
        self.resolver = ContextualPolarityResolver.load(dict_dir)
        # 필터가 꺼져 있으면 사전을 읽지 않는다. 그래야 패턴 파일 없이도
        # exclude_boilerplate=False 설정으로 동작시킬 수 있다.
        self.boilerplate = (
            BoilerplateFilter.load(dict_dir)
            if self.config.exclude_boilerplate
            else BoilerplateFilter({})
        )
        self.seo = SeoChecker(seo_config)
        morphology.get_kiwi()  # 첫 호출 지연을 초기화 시점으로 옮긴다

    def check(
        self,
        text: str,
        keyword: str | None = None,
        seo_config: SeoConfig | None = None,
    ) -> QualityReport:
        """초안을 검사한다.

        text는 마크다운 원문을 그대로 넘긴다. SEO 구조 검사는 원문을,
        나머지 검사는 마크다운을 제거한 평문을 대상으로 한다.
        keyword는 노션 '매칭키워드' 속성 값을 넘기면 배치 검사가 활성화된다.

        seo_config를 넘기면 이 호출에 한해 구조 임계값을 덮어쓴다.
        목표 분량과 챕터 수는 소재 정보량에 따라 초안마다 달라지므로,
        build_prompt()가 돌려준 config를 그대로 넘기면 프롬프트가 지시한
        값과 검사가 강제하는 값이 어긋나지 않는다.

        SeoChecker는 정규식이 모듈 레벨이고 인스턴스는 config만 들고 있어
        호출마다 새로 만들어도 비용이 없다. Kiwi·사전 로딩과 달리 검사기
        인스턴스를 재사용할 이유가 없는 부분이다.
        """
        seo_checker = SeoChecker(seo_config) if seo_config is not None else self.seo

        # SEO 구조는 마크다운이 살아 있어야 검사할 수 있다.
        seo = seo_checker.check(text, keyword) if self.config.check_seo else None

        body = strip_markdown(text) if self.config.strip_markdown else text

        # 캡션·출처 등 템플릿 문구를 먼저 걷어낸다.
        # 형태소 분석 전에 제거해야 감정 비율과 키워드 통계가 오염되지 않는다.
        if self.config.exclude_boilerplate:
            body, excluded = self.boilerplate.apply(body)
        else:
            excluded = []

        sentences = morphology.analyze(body)
        stats = morphology.summarize(sentences, body, top_k=self.config.top_keywords)
        sentiment = analyze_sentiment(
            sentences, self.sentiment_dict, resolver=self.resolver
        )
        hits = self.banned.check(body, sentences)

        warnings = self._build_warnings(stats, sentiment, body)

        # 구조 경고를 한 곳으로 모은다. 파이프라인이 report.warnings만 보고
        # 분기해도 구조 문제를 놓치지 않게 하기 위함이다. 원본은 report.seo에
        # 그대로 남아 있어 구조 경고만 따로 꺼내 쓸 수도 있다.
        if seo is not None:
            warnings.extend(f"[구조] {w}" for w in seo.warnings)

        passed = not any(h.severity is Severity.BLOCK for h in hits)

        return QualityReport(
            passed=passed,
            banned_words=hits,
            sentiment=sentiment,
            morphology=stats,
            warnings=warnings,
            char_count=len(body),
            excluded_lines=excluded,
            seo=seo,
        )

    def _build_warnings(self, stats, sentiment, body: str) -> list[str]:
        cfg = self.config
        warnings: list[str] = []

        # SEO 검사가 켜져 있으면 길이 경고는 그쪽에서 더 정확히 낸다.
        if not cfg.check_seo and len(body) < cfg.min_char_count:
            warnings.append(
                f"본문이 짧습니다: {len(body)}자 (권장 {cfg.min_char_count}자 이상)"
            )

        for kw in stats.top_keywords:
            if stats.noun_count < cfg.keyword_density_min_nouns:
                break
            if (
                kw.density > cfg.max_keyword_density
                and kw.count >= cfg.keyword_density_min_count
            ):
                warnings.append(
                    f"키워드 과최적화 의심: '{kw.word}' {kw.count}회 "
                    f"(밀도 {kw.density:.1%}, 기준 {cfg.max_keyword_density:.0%})"
                )

        if stats.total_tokens and stats.lexical_diversity < cfg.min_lexical_diversity:
            warnings.append(
                f"어휘 다양성 부족: {stats.lexical_diversity:.2f} "
                f"(기준 {cfg.min_lexical_diversity:.2f})"
            )

        if stats.style.dominant != "unknown" and stats.style.consistency < cfg.min_style_consistency:
            dist = ", ".join(f"{k} {v}회" for k, v in stats.style.distribution.items())
            warnings.append(
                f"문체 혼용: 지배 문체 '{stats.style.dominant}' "
                f"{stats.style.consistency:.1%} ({dist})"
            )

        if sentiment.negative_ratio > cfg.max_negative_ratio:
            warnings.append(
                f"부정 문장 비율이 높습니다: {sentiment.negative_ratio:.1%} "
                f"(기준 {cfg.max_negative_ratio:.0%}) — 도메인 예외 사전 점검 필요"
            )

        if stats.avg_sentence_length > cfg.max_avg_sentence_length:
            warnings.append(
                f"문장이 깁니다: 평균 {stats.avg_sentence_length:.1f}자 "
                f"(기준 {cfg.max_avg_sentence_length:.0f}자)"
            )

        return warnings


def format_report(report: QualityReport, max_hits: int = 20) -> str:
    """사람이 읽을 요약 텍스트. Slack 알림·Notion 본문에 그대로 붙일 수 있다."""
    lines: list[str] = []
    status = "PASS" if report.passed else "BLOCKED"
    # char_count는 줄바꿈·빈 줄을 포함한 원시 길이다. 아래 '구조' 항목의
    # 본문 글자 수(seo.body_chars)는 줄바꿈을 뺀 값이라 서로 다르다.
    # 네이버 형식은 20자마다 줄을 끊어 격차가 20%를 넘기도 한다.
    # 프롬프트가 지시하는 분량 기준은 seo.body_chars 쪽이다.
    lines.append(f"[{status}] 원시 {report.char_count}자 / "
                 f"{report.morphology.sentence_count}문장")
    lines.append("")

    s = report.sentiment
    lines.append("■ 감정 비율")
    lines.append(
        f"  긍정 {s.positive_ratio:.1%} / 중립 {s.neutral_ratio:.1%} / "
        f"부정 {s.negative_ratio:.1%} (평균 점수 {s.average_score:+.2f})"
    )
    if s.binary_positive_ratio is None:
        lines.append("  극성 비율: 감정 문장 없음 (참고 지표)")
    else:
        lines.append(
            f"  극성 비율 {s.binary_positive_ratio:.1%} "
            f"(긍정 {s.positive_count} : 부정 {s.negative_count}) — 참고 지표"
        )
    lines.append("")

    m = report.morphology
    lines.append("■ 형태소 분석")
    lines.append(f"  형태소 {m.total_tokens}개 / 고유 {m.unique_tokens}개 "
                 f"/ 어휘 다양성 {m.lexical_diversity:.2f}")
    lines.append(f"  문체 {m.style.dominant} (일관성 {m.style.consistency:.1%})")
    top = ", ".join(f"{k.word}({k.count})" for k in m.top_keywords[:8])
    lines.append(f"  주요 키워드: {top}")
    lines.append("")

    lines.append(f"■ 금칙어 {len(report.banned_words)}건")
    if not report.banned_words:
        lines.append("  없음")
    for hit in report.banned_words[:max_hits]:
        mark = "X" if hit.severity is Severity.BLOCK else "!"
        lines.append(
            f"  [{mark}] {hit.matched} ({hit.category_label}/{hit.match_type}) "
            f"@{hit.start} — {hit.context}"
        )
    if len(report.banned_words) > max_hits:
        lines.append(f"  … 외 {len(report.banned_words) - max_hits}건")
    lines.append("")

    lines.append(f"■ 경고 {len(report.warnings)}건")
    if not report.warnings:
        lines.append("  없음")
    for w in report.warnings:
        lines.append(f"  - {w}")

    if report.seo is not None:
        s2 = report.seo
        lines.append(f"■ 구조 ({s2.format})")
        lines.append(
            f"  제목 {s2.title_length}자 / 챕터 {s2.chapter_count}개 / "
            f"순수 본문 {s2.body_chars}자 (줄바꿈 제외)"
        )
        if s2.format == "naver_blog":
            lines.append(
                f"  줄 {s2.line_count}개 / 20자 이하 {s2.short_line_ratio:.0%} / "
                f"최장 {s2.longest_line}자"
            )
        if s2.keyword:
            marks = [
                ("제목", s2.keyword_in_title),
                ("도입부", s2.keyword_in_intro),
                ("챕터", s2.keyword_in_chapters > 0),
            ]
            placed = " ".join(f"{n}{'O' if v else 'X'}" for n, v in marks)
            lines.append(f"  키워드 '{s2.keyword}' 배치: {placed}")
        # 구조 경고는 위 '경고' 항목에 [구조] 접두사로 이미 합쳐져 있다.
        lines.append("")

    if report.excluded_lines:
        lines.append("")
        lines.append(f"■ 분석 제외 {len(report.excluded_lines)}줄 (캡션·출처 등)")
        for e in report.excluded_lines:
            lines.append(f"  L{e.line_number} [{e.rule}] {e.text[:60]}")

    return "\n".join(lines)
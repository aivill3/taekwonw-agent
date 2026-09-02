"""품질 측정 어댑터 (측정 전용 — 발행을 막지 않는다).

목적
----
현재 writer.py가 만드는 마크다운 초안이 실제로 어떤 지표를 갖는지
누적 측정한다. 네이버 형식으로 전환할지(=writer.py를 drafting 패키지로
교체할지) 판단할 근거를 만드는 것이 이 모듈의 존재 이유다.

"산업 SEO 규칙은 실측 전까지 통설"이라는 원칙을 그대로 적용한다.
전환 작업량이 크므로, 먼저 재고 나서 결정한다.

측정하는 것
----------
1. 형식 무관 지표 — 문체·감정·금칙어·어휘
   strip_markdown() 이후 평문 기준이라 마크다운/네이버 어느 쪽이든 같다.
   지금 당장 유효한 품질 신호다.

2. 현행 형식 점수 — Format.MARKDOWN 기준 구조 검사
   writer.py가 실제로 지시받은 값(제목 25~50자, 섹션=소주제 수)으로 검사한다.
   SeoConfig 기본값(제목 15~40자)은 네이버 실측치라 여기 쓰면 안 된다.

3. 네이버 전환 거리 — Format.NAVER_BLOG 기준 같은 텍스트 재검사
   숫자 챕터 0개, 헤딩 다수, 20자 이하 줄 비율 낮음이 나올 것이다.
   이 격차의 크기가 전환 비용의 대리 지표다.

측정 시점
--------
publish_pipeline.run()의 3단계(write_all) 직후, 삽화 확보 전이다.
따라서 초안에는 이미지가 아직 없다. min_images=0으로 두는 이유다.

주의: 글자 수 단위가 두 가지다
-----------------------------
  writer.SCHEMA_MAX_LENGTH(2000)  = len(markdown). 줄바꿈·'## ' 기호 포함
  SeoReport.body_chars     = 유효 줄 길이 합. 줄바꿈 제외
후자가 항상 작다. 두 숫자를 나란히 기록하되 섞어 쓰지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
import re
from datetime import datetime
from typing import Any

from config.settings import DATA_DIR, KST
from core.logger import get_logger
from agents.quality import CheckConfig, QualityChecker, format_report
from agents.quality.quality_models import QualityReport, Severity
from agents.quality.seo_checker import Format, SeoChecker, SeoConfig
from config.draft_config import SCHEMA_MAX_LENGTH, SCHEMA_MIN_TARGET

log = get_logger(__name__)

METRICS_DIR = DATA_DIR / "quality"
METRICS_PATH = METRICS_DIR / "metrics.jsonl"

# 측정 단계에서는 어떤 경우에도 파이프라인을 멈추지 않는다.
# 금칙어 BLOCK이 떠도 로그와 Slack 알림으로만 알린다. 게이트로 승격하려면
# 먼저 오탐률을 누적 측정해야 한다.
BLOCK_ON_FAILURE = False


@dataclass
class DraftMetrics:
    """초안 1편의 측정 결과."""

    page_id: str
    title: str
    model: str
    report: QualityReport
    markdown_chars: int  # len(markdown) — writer.py 기준
    naver_gap: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return not self.report.passed

    def to_row(self) -> dict[str, Any]:
        """JSONL 한 줄. 나중에 pandas로 읽어 추세를 본다."""
        r = self.report
        m = r.morphology
        s = r.sentiment
        seo = r.seo
        return {
            "ts": datetime.now(KST).isoformat(timespec="seconds"),
            "page_id": self.page_id,
            "title": self.title,
            "model": self.model,
            # --- 분량 (단위 두 가지를 모두 남긴다) ---
            "markdown_chars": self.markdown_chars,
            "plain_chars": r.char_count,
            "body_chars": seo.body_chars if seo else None,
            # --- 구조 (현행 마크다운 기준) ---
            "title_length": seo.title_length if seo else None,
            "chapter_count": seo.chapter_count if seo else None,
            "paragraph_count": seo.paragraph_count if seo else None,
            "longest_paragraph": seo.longest_paragraph if seo else None,
            # --- 문체 (형식 무관) ---
            "style_dominant": m.style.dominant,
            "style_consistency": m.style.consistency,
            "style_distribution": m.style.distribution,
            "lexical_diversity": m.lexical_diversity,
            "avg_sentence_length": m.avg_sentence_length,
            "sentence_count": m.sentence_count,
            "noun_count": m.noun_count,
            "top_keywords": [
                {"w": k.word, "n": k.count, "d": k.density} for k in m.top_keywords[:10]
            ],
            # --- 감정 ---
            "positive_ratio": s.positive_ratio,
            "negative_ratio": s.negative_ratio,
            "neutral_ratio": s.neutral_ratio,
            # --- 금칙어 ---
            "banned_block": sum(
                1 for h in r.banned_words if h.severity is Severity.BLOCK
            ),
            "banned_warn": sum(
                1 for h in r.banned_words if h.severity is not Severity.BLOCK
            ),
            "banned_words": [h.matched for h in r.banned_words[:20]],
            # --- 경고 ---
            "warnings": r.warnings,
            # --- 네이버 전환 거리 ---
            "naver_gap": self.naver_gap,
        }


# 발행용 블록 마커. 사람이 서식을 적용할 위치 표시일 뿐 글의 일부가 아니다.
# 남겨두면 Kiwi가 '본문'을 실제 단어로 세어(실측: 주요 키워드 7~8위) 형태소
# 총량·어휘 다양성·키워드 밀도가 모두 왜곡된다.
_MARKER = re.compile(r"/\*\s*(?:소제목|본문|인용구)\s*\*/")


def strip_markers(text: str) -> str:
    """측정 전에 블록 마커를 제거한다. 줄 구조는 건드리지 않는다."""
    return "\n".join(_MARKER.sub("", line).rstrip() for line in text.split("\n"))


def build_seo_config(item: dict, markdown: str) -> SeoConfig:
    """현행 writer.py가 실제로 지시받은 값으로 검사 설정을 만든다.

    SeoConfig 기본값은 네이버 실측치(제목 20~22자, 본문 1,825~2,102자)라
    마크다운 초안에 그대로 적용하면 전부 경고가 뜬다. 측정 목적에 맞게
    writer.py의 프롬프트·상수와 같은 기준으로 맞춘다.
    """
    # 작성기가 프롬프트에 지시한 값이 있으면 그대로 쓴다.
    # 지시값과 검사 임계값이 어긋나면 정상 초안이 매번 경고로 잡힌다.
    preset = item.get("seo_config")
    if preset is not None:
        return preset

    body = item.get("body", "") or ""
    # schema_draft_agent.target_length()와 동일한 계산.
    # 함수를 임포트하면 quality 가 drafting 을 의존하게 되므로 식만 옮겼다.
    from config.draft_config import SCHEMA_LENGTH_RATIO, SCHEMA_TARGET_LENGTH
    target = max(
        SCHEMA_MIN_TARGET,
        min(SCHEMA_TARGET_LENGTH, int(len(body) * SCHEMA_LENGTH_RATIO)),
    )

    return SeoConfig(
        format=Format.NAVER_BLOG,
        # writer.py 프롬프트: "title: 25~50자"
        title_min=25,
        title_max=50,
        # 하한은 writer.py의 SCHEMA_MIN_TARGET, 상한은 Notion 속성 하드 리밋.
        # 단위가 len(markdown)이 아니라 body_chars라 실제로는 더 여유가 있다.
        body_min_chars=min(SCHEMA_MIN_TARGET, target),
        body_max_chars=SCHEMA_MAX_LENGTH,
        # 섹션 1개 = 소주제 1개. 승인 게이트에서 정해진 수를 그대로 쓴다.
        min_chapters=max(1, len(item.get("subtopics", []))),
        # 삽화는 이 시점 이후(collect_images)에 붙는다. 여기서 세면 항상 0이다.
        min_images=0,
    )


def measure_naver_gap(markdown: str, keyword: str | None) -> dict[str, Any]:
    """같은 텍스트를 네이버 형식 기준으로 재검사해 전환 거리를 잰다.

    지금은 마크다운이므로 대부분 미달로 나오는 게 정상이다.
    중요한 건 통과 여부가 아니라 격차의 크기다. 예를 들어 20자 이하 줄
    비율이 30%면 줄바꿈 규칙만 적용해도 상당 부분 해결된다는 뜻이고,
    5%면 문장 구조부터 다시 써야 한다는 뜻이다.
    """
    checker = SeoChecker(SeoConfig(format=Format.NAVER_BLOG, min_images=0))
    r = checker.check(markdown, keyword)
    return {
        "numeric_chapters": r.chapter_count,  # 마크다운 초안에선 0일 것
        "heading_count": r.heading_count,  # 제거해야 할 '## ' 개수
        "bullet_count": r.bullet_count,
        "short_line_ratio": r.short_line_ratio,  # 20자 이하 줄 비율
        "longest_line": r.longest_line,
        "line_count": r.line_count,
        "body_chars": r.body_chars,
    }


def _keyword_of(item: dict) -> str | None:
    """노션 '매칭키워드'. 파이프라인 dict에 없을 수 있어 방어적으로 읽는다."""
    for key in ("keyword", "matched_keyword", "매칭키워드"):
        val = item.get(key)
        if val:
            return str(val).strip()
    return None


def measure_all(items: list[dict]) -> list[DraftMetrics]:
    """작성된 초안들을 측정한다. 실패해도 예외를 밖으로 던지지 않는다.

    측정은 부가 기능이다. 여기서 난 오류가 발행을 막으면 안 된다.
    """
    if not items:
        return []

    try:
        # Kiwi 초기화와 사전 로딩 비용이 있어 한 번만 만든다.
        checker = QualityChecker(CheckConfig(check_seo=True))
    except Exception as e:
        log.warning(f"품질 검사기 초기화 실패 — 측정을 건너뜁니다: {e}")
        return []

    results: list[DraftMetrics] = []
    for item in items:
        markdown = item.get("markdown", "")
        if not markdown:
            continue
        try:
            keyword = _keyword_of(item)
            # 마커를 뺀 텍스트로 잰다. 줄 수·분량 지표도 마커 없는 상태가 맞다.
            measured = strip_markers(markdown)
            report = checker.check(
                measured, keyword, seo_config=build_seo_config(item, measured)
            )
            results.append(
                DraftMetrics(
                    page_id=item.get("page_id", ""),
                    title=item.get("title", "")[:60],
                    model=item.get("model", ""),
                    report=report,
                    markdown_chars=len(markdown),
                    naver_gap=measure_naver_gap(measured, keyword),
                )
            )
        except Exception as e:
            log.warning(f"품질 측정 실패 ({item.get('title', '')[:30]}): {e}")

    return results


def append_metrics(metrics: list[DraftMetrics]) -> None:
    """측정 결과를 JSONL로 누적한다. 한 줄 = 초안 한 편."""
    if not metrics:
        return
    try:
        METRICS_DIR.mkdir(parents=True, exist_ok=True)
        with METRICS_PATH.open("a", encoding="utf-8") as f:
            for m in metrics:
                f.write(json.dumps(m.to_row(), ensure_ascii=False) + "\n")
        log.info(f"품질 지표 {len(metrics)}건 기록: {METRICS_PATH.name}")
    except Exception as e:
        log.warning(f"품질 지표 기록 실패: {e}")


def format_summary(metrics: list[DraftMetrics]) -> str:
    """실행 1회분 요약. 로그와 dry-run 출력에 쓴다."""
    if not metrics:
        return "(측정된 초안 없음)"

    n = len(metrics)
    lines = [f"■ 품질 측정 {n}건 (측정 전용 — 발행은 막지 않습니다)"]

    def avg(fn) -> float:
        return sum(fn(m) for m in metrics) / n

    lines.append(
        f"  문체 일관성 평균 {avg(lambda m: m.report.morphology.style.consistency):.1%}"
        f" / 어휘 다양성 {avg(lambda m: m.report.morphology.lexical_diversity):.2f}"
    )
    styles: dict[str, int] = {}
    for m in metrics:
        styles[m.report.morphology.style.dominant] = (
            styles.get(m.report.morphology.style.dominant, 0) + 1
        )
    lines.append(f"  지배 문체 분포: {styles}")
    lines.append(
        f"  분량 평균: 마크다운 {avg(lambda m: m.markdown_chars):.0f}자"
        f" / 순수 본문 {avg(lambda m: m.report.seo.body_chars if m.report.seo else 0):.0f}자"
    )

    blocked = [m for m in metrics if m.blocked]
    total_banned = sum(len(m.report.banned_words) for m in metrics)
    lines.append(f"  금칙어 적발 {total_banned}건 (차단급 초안 {len(blocked)}편)")
    for m in blocked:
        hits = ", ".join(
            h.matched for h in m.report.banned_words if h.severity is Severity.BLOCK
        )
        lines.append(f"    [차단급] {m.title[:40]} — {hits}")

    warn_total = sum(len(m.report.warnings) for m in metrics)
    lines.append(f"  경고 {warn_total}건")

    # 네이버 전환 거리
    lines.append("  ── 네이버 형식 전환 거리 ──")
    lines.append(
        f"  제거할 '## ' 헤딩 평균 {avg(lambda m: m.naver_gap.get('heading_count', 0)):.1f}개"
        f" / 숫자 챕터 {avg(lambda m: m.naver_gap.get('numeric_chapters', 0)):.1f}개"
    )
    lines.append(
        f"  20자 이하 줄 비율 {avg(lambda m: m.naver_gap.get('short_line_ratio', 0)):.0%}"
        f" (목표 90%) / 최장 줄 평균 {avg(lambda m: m.naver_gap.get('longest_line', 0)):.0f}자"
    )
    return "\n".join(lines)


def detail_reports(metrics: list[DraftMetrics]) -> str:
    """초안별 상세 리포트. dry-run에서만 출력한다."""
    blocks = []
    for m in metrics:
        blocks.append(f"───── {m.title} ─────\n{format_report(m.report)}")
    return "\n\n".join(blocks)

def notion_summary(m: DraftMetrics) -> str:
    """노션 '형태소' 속성에 넣을 요약.

    detail_reports() 는 콘솔용이라 구분선과 여백이 많다. 속성은 2,000자
    상한이라 같은 내용을 넣으면 잘리므로, 판단에 필요한 것만 추린다.
      감정 비율 / 형태소 통계 / 금칙어 / 구조 경고
    """
    r = m.report
    mo = r.morphology
    se = r.sentiment
    seo = r.seo
    lines: list[str] = [f"[{'차단' if m.blocked else '통과'}] {r.char_count}자"]

    if se:
        lines.append(
            f"■ 감정 긍정 {se.positive_ratio:.1%} / 중립 {se.neutral_ratio:.1%} "
            f"/ 부정 {se.negative_ratio:.1%} (평균 {se.average_score:+.2f})"
        )

    if mo:
        lines.append(
            f"■ 형태소 {mo.total_tokens}개 / 고유 {mo.unique_tokens}개 "
            f"/ 어휘 다양성 {mo.lexical_diversity:.2f}"
        )
        lines.append(
            f"  문체 {mo.style.dominant} (일관성 {mo.style.consistency:.1%}) "
            f"· 평균 문장 {mo.avg_sentence_length:.1f}자"
        )
        if mo.top_keywords:
            kw = ", ".join(f"{k.word}({k.count})" for k in mo.top_keywords[:8])
            lines.append(f"  주요 키워드 {kw}")

    if r.banned_words:
        lines.append(f"■ 금칙어 {len(r.banned_words)}건")
        for b in r.banned_words[:5]:
            lines.append(f"  [X] {b.matched} ({b.category_label})")

    if seo:
        lines.append(
            f"■ 구조 제목 {seo.title_length}자 / 챕터 {seo.chapter_count}개 "
            f"/ 본문 {seo.body_chars}자 / 20자 이하 줄 {seo.short_line_ratio:.0%}"
        )

    if r.warnings:
        lines.append(f"■ 경고 {len(r.warnings)}건")
        lines.extend(f"  - {w}" for w in r.warnings[:6])

    return "\n".join(lines)
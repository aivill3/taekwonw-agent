"""글 구조·형식 검사.

prompts/draft/writing_guide.md 의 체크리스트 중 기계로
확인 가능한 항목을 검증한다. 가이드가 규칙을 정의하고 이 모듈이 강제하는
구조라, 가이드를 고치면 여기 임계값도 함께 맞춰야 한다.
경로 상수는 config/settings.py 의 GUIDE_DIR 하나뿐이므로, 이 문서 경로를
옮길 때는 그쪽을 고친다.

분량·챕터 수 임계값은 초안마다 달라진다. drafting.prompt_builder가
소재 정보량으로 SeoConfig를 만들어 넘기므로, 여기 기본값은 그 주입이
없을 때의 폴백이다.

두 가지 형식을 지원한다.

    NAVER_BLOG (기본)
        네이버 블로그 에디터용. 숫자 챕터로 구분하고 한 줄을 15~20자로 끊는다.
        마크다운 헤딩과 불릿은 권장이 아니라 **금지** 대상이다.

    MARKDOWN
        일반 마크다운. ## 소제목과 ![]() 이미지를 센다.

태권월드 기존 발행 글 2편을 측정한 결과 마크다운 헤딩 0개, 불릿 0개,
줄 길이 평균 12자(98%가 20자 이하), 숫자 챕터 4개였다.
기본값을 NAVER_BLOG로 둔 이유다.

**원문을 대상으로 한다.** strip_markdown()을 거치면 헤딩·이미지 구문과
줄바꿈이 사라져 구조를 셀 수 없다. 감정 분석·금칙어 검사가 평문을 쓰는
것과 대비된다.

모든 결과는 경고이며 발행을 차단하지 않는다. 구조 미달은 법적 위험이
아니라 품질 문제이고, 짧은 속보처럼 규칙을 벗어나는 것이 맞는 글도 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from config.quality_config import Format, SeoConfig

# 숫자 챕터. "1. 첫 번째는", "2.", "3 " 등 줄 첫머리의 숫자를 잡는다.
# 뒤에 문자가 이어져도 되지만, 연·월·일 같은 날짜 표기는 제외한다.
_NUM_CHAPTER = re.compile(r"^\s*(\d{1,2})[.)]?(?:\s|$)(?!\s*[년월일원명개])", re.MULTILINE)

_BULLET = re.compile(r"^\s{0,3}[-*•]\s+", re.MULTILINE)

# 번호 없이 순서만 쓴 챕터. "첫 번째는", "두 번째는" 등.
# LLM이 숫자를 빠뜨리는 흔한 실패라 별도로 감지해 안내한다.
_ORDINAL_ONLY = re.compile(
    r"^\s*(첫|두|세|네|다섯|여섯|일곱)\s*번째(는|로|으로)?", re.MULTILINE
)
_H1 = re.compile(r"^\s{0,3}#\s+(.+)$", re.MULTILINE)
_H2 = re.compile(r"^\s{0,3}##\s+(.+)$", re.MULTILINE)
_H3 = re.compile(r"^\s{0,3}###\s+(.+)$", re.MULTILINE)
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+.*$", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
# 발행용 블록 마커. `/* 소제목 */` 이 붙은 줄이 챕터의 시작이다.
_SUBHEAD_MARKER = re.compile(r"/\*\s*소제목\s*\*/")
# 숫자만 있는 줄 = 이미지 자리. 챕터로 세면 안 된다.
_IMAGE_SLOT = re.compile(r"^\s*\d{1,2}\s*$")

# 네이버 에디터가 빈 줄에 넣는 제로폭 공백
_ZWSP = "\u200b"


@dataclass
class SeoReport:
    """구조 검사 결과."""

    format: str
    title: str | None
    title_length: int
    chapter_count: int
    chapters: list[str]
    body_chars: int
    line_count: int = 0
    short_line_ratio: float = 0.0
    longest_line: int = 0
    heading_count: int = 0
    bullet_count: int = 0
    image_count: int = 0
    paragraph_count: int = 0
    longest_paragraph: int = 0
    keyword: str | None = None
    keyword_in_title: bool = False
    keyword_in_title_head: bool = False
    keyword_in_intro: bool = False
    keyword_in_chapters: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "title": self.title,
            "title_length": self.title_length,
            "chapter_count": self.chapter_count,
            "chapters": self.chapters,
            "body_chars": self.body_chars,
            "line_count": self.line_count,
            "short_line_ratio": self.short_line_ratio,
            "longest_line": self.longest_line,
            "heading_count": self.heading_count,
            "bullet_count": self.bullet_count,
            "image_count": self.image_count,
            "paragraph_count": self.paragraph_count,
            "longest_paragraph": self.longest_paragraph,
            "keyword": self.keyword,
            "keyword_in_title": self.keyword_in_title,
            "keyword_in_title_head": self.keyword_in_title_head,
            "keyword_in_intro": self.keyword_in_intro,
            "keyword_in_chapters": self.keyword_in_chapters,
            "warnings": self.warnings,
        }


def _effective_lines(text: str) -> list[str]:
    """제로폭 공백만 있는 줄과 빈 줄을 제외한 실제 내용 줄."""
    result = []
    for line in text.split("\n"):
        stripped = line.replace(_ZWSP, "").strip()
        if stripped:
            result.append(stripped)
    return result


class SeoChecker:
    """초안의 형식·구조를 검사한다."""

    def __init__(self, config: SeoConfig | None = None) -> None:
        self.config = config or SeoConfig()

    # --- 파싱 -------------------------------------------------------------

    @staticmethod
    def _extract_title(text: str) -> str | None:
        """H1이 있으면 제목으로, 없으면 첫 내용 줄을 제목으로 본다."""
        m = _H1.search(text)
        if m:
            return m.group(1).strip()
        lines = _effective_lines(text)
        return lines[0] if lines else None

    def _extract_chapters(self, text: str) -> list[str]:
        """형식에 따라 챕터 목록을 뽑는다."""
        if self.config.format is Format.MARKDOWN:
            return [h.strip() for h in _H2.findall(text)] + [
                h.strip() for h in _H3.findall(text)
            ]
        # 1순위: 블록 마커. 챕터 줄에 숫자를 붙이지 않게 바뀌었으므로
        # (가이드 2장) 숫자 패턴으로는 챕터를 찾을 수 없다.
        marked = [
            _SUBHEAD_MARKER.sub("", line).strip()
            for line in _effective_lines(text)
            if _SUBHEAD_MARKER.search(line)
        ]
        if marked:
            return marked

        # 2순위: 순서 표현. 측정 단계에서 마커를 걷어낸 텍스트가 들어오므로
        # (quality_gate.strip_markers) 마커 없이도 챕터를 찾을 수 있어야 한다.
        ordinal = [
            line.strip()
            for line in _effective_lines(text)
            if _ORDINAL_ONLY.match(line.strip())
        ]
        if ordinal:
            return ordinal

        # 3순위: 예전 형식(번호 챕터) 호환.
        # 이미지 자리(숫자만 있는 줄)는 제외한다 — 포함하면 챕터 수가
        # 실제의 3배 이상으로 부풀어 min_chapters 검사가 무의미해진다.
        chapters = []
        for line in _effective_lines(text):
            if _IMAGE_SLOT.match(line):
                continue
            if _NUM_CHAPTER.match(line):
                chapters.append(line)
        return chapters

    @staticmethod
    def _paragraphs(text: str) -> list[str]:
        body = _ANY_HEADING.sub("", text)
        body = _IMAGE.sub("", body)
        result = []
        for block in re.split(r"\n\s*\n", body):
            joined = " ".join(block.replace(_ZWSP, "").split())
            if joined and not joined.startswith(">"):
                result.append(joined)
        return result

    # --- 검사 -------------------------------------------------------------

    def check(self, text: str, keyword: str | None = None) -> SeoReport:
        """원문과 핵심 키워드를 받아 구조를 검사한다.

        keyword는 노션의 '매칭키워드' 속성 값을 넘기면 된다.
        None이면 키워드 관련 검사를 건너뛴다.
        """
        cfg = self.config
        lines = _effective_lines(text)
        title = self._extract_title(text)
        chapters = self._extract_chapters(text)
        paragraphs = self._paragraphs(text)
        body_chars = sum(len(line) for line in lines)

        short = sum(1 for line in lines if len(line) <= cfg.max_line_chars)

        report = SeoReport(
            format=cfg.format.value,
            title=title,
            title_length=len(title) if title else 0,
            chapter_count=len(chapters),
            chapters=chapters,
            body_chars=body_chars,
            line_count=len(lines),
            short_line_ratio=round(short / len(lines), 4) if lines else 0.0,
            longest_line=max((len(line) for line in lines), default=0),
            heading_count=len(_ANY_HEADING.findall(text)),
            bullet_count=len(_BULLET.findall(text)),
            image_count=len(_IMAGE.findall(text)),
            paragraph_count=len(paragraphs),
            longest_paragraph=max((len(p) for p in paragraphs), default=0),
            keyword=keyword,
        )

        self._check_title(report)
        self._check_body(report)
        self._check_chapters(report, text)
        if cfg.format is Format.NAVER_BLOG:
            self._check_naver_format(report)
        else:
            self._check_markdown_format(report, paragraphs, text)
        if keyword:
            self._check_keyword(report, paragraphs)

        return report

    def _check_title(self, r: SeoReport) -> None:
        cfg = self.config
        if r.title is None:
            r.warnings.append("제목을 찾을 수 없습니다")
            return
        if r.title_length < cfg.title_min:
            r.warnings.append(
                f"제목이 짧습니다: {r.title_length}자 (최소 {cfg.title_min}자)"
            )
        elif r.title_length > cfg.title_max:
            r.warnings.append(
                f"제목이 깁니다: {r.title_length}자 — 모바일에서 뒷부분이 잘립니다"
            )

    def _check_body(self, r: SeoReport) -> None:
        cfg = self.config
        if r.body_chars < cfg.body_min_chars:
            r.warnings.append(
                f"본문이 짧습니다: {r.body_chars}자 (권장 {cfg.body_min_chars}자 이상)"
            )
        elif r.body_chars > cfg.body_max_chars:
            r.warnings.append(
                f"본문이 깁니다: {r.body_chars}자 (상한 {cfg.body_max_chars}자)"
            )

    def _check_chapters(self, r: SeoReport, text: str = "") -> None:
        cfg = self.config
        label = "소제목" if cfg.format is Format.MARKDOWN else "숫자 챕터"
        if r.chapter_count >= cfg.min_chapters:
            return

        msg = (
            f"{label} 부족: {r.chapter_count}개 "
            f"(권장 {cfg.min_chapters}개 이상)"
        )
        # 숫자만 빠뜨린 경우라면 원인을 짚어준다.
        if cfg.format is Format.NAVER_BLOG and text:
            ordinals = len(_ORDINAL_ONLY.findall(text))
            if ordinals >= cfg.min_chapters:
                msg += (
                    f" — 번호 없는 순서 표현이 {ordinals}개 있습니다. "
                    f"'첫 번째는' 앞에 '1.'을 붙이세요"
                )
        r.warnings.append(msg)

    def _check_naver_format(self, r: SeoReport) -> None:
        """네이버 블로그 형식 검사.

        마크다운 헤딩과 불릿은 에디터에서 그대로 노출되므로 금지 대상이다.
        """
        cfg = self.config
        if r.heading_count:
            r.warnings.append(
                f"마크다운 헤딩 {r.heading_count}개 — 네이버 블로그에서는 "
                f"숫자 챕터로 구분합니다"
            )
        if r.bullet_count:
            r.warnings.append(
                f"불릿 리스트 {r.bullet_count}개 — 네이버 블로그에서는 사용하지 않습니다"
            )
        if r.line_count and r.short_line_ratio < cfg.min_short_line_ratio:
            r.warnings.append(
                f"줄바꿈 규칙 미달: {cfg.max_line_chars}자 이하 줄이 "
                f"{r.short_line_ratio:.0%} (권장 {cfg.min_short_line_ratio:.0%} 이상, "
                f"최장 {r.longest_line}자)"
            )

    def _check_markdown_format(
        self, r: SeoReport, paragraphs: list[str], text: str
    ) -> None:
        cfg = self.config
        over = [len(p) for p in paragraphs if len(p) > cfg.max_paragraph_chars]
        if over:
            r.warnings.append(
                f"긴 단락 {len(over)}개 (최대 {max(over)}자, "
                f"권장 {cfg.max_paragraph_chars}자 이하)"
            )
        if r.image_count < cfg.min_images:
            r.warnings.append(
                f"이미지가 부족합니다: {r.image_count}장 (권장 {cfg.min_images}장 이상)"
            )
        for alt, _ in _IMAGE.findall(text):
            alt = alt.strip()
            if not alt:
                r.warnings.append("대체 텍스트가 비어 있는 이미지가 있습니다")
            elif len(alt) < cfg.caption_min:
                r.warnings.append(f"대체 텍스트가 짧습니다 ({len(alt)}자): '{alt}'")
            elif len(alt) > cfg.caption_max:
                r.warnings.append(f"대체 텍스트가 깁니다 ({len(alt)}자): '{alt[:30]}…'")

    def _check_keyword(self, r: SeoReport, paragraphs: list[str]) -> None:
        cfg = self.config
        kw = (r.keyword or "").strip()
        if not kw:
            return

        def norm(s: str) -> str:
            return s.replace(" ", "")

        nkw = norm(kw)

        if r.title:
            ntitle = norm(r.title)
            r.keyword_in_title = nkw in ntitle
            if r.keyword_in_title:
                r.keyword_in_title_head = ntitle.index(nkw) <= cfg.title_keyword_head
            else:
                r.warnings.append(f"제목에 핵심 키워드가 없습니다: '{kw}'")

        intro = " ".join(paragraphs[:2]) if paragraphs else ""
        r.keyword_in_intro = nkw in norm(intro)
        if not r.keyword_in_intro:
            r.warnings.append(f"도입부에 핵심 키워드가 없습니다: '{kw}'")

        r.keyword_in_chapters = sum(1 for c in r.chapters if nkw in norm(c))
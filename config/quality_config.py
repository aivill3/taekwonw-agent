"""품질 검사 임계값.

검사 로직이 아니라 '기준값'만 둔다. 판정 코드는 agents/quality/ 에 있다.

여기로 올린 이유
---------------
초안 생성(agents/drafting)이 SeoConfig를 만들어 검사기에 넘긴다.
분량·챕터 수는 소재 정보량에 따라 초안마다 달라지므로, 프롬프트에 지시한
값을 그대로 검사 임계값으로 써야 "1,300자로 쓰세요" 라고 시킨 뒤
"1,600자 미만입니다" 라고 경고하는 모순이 생기지 않는다.

설정이 agents/quality 안에 있으면 drafting 이 quality 를 임포트하게 되어
에이전트끼리 얽힌다. 설정만 config 로 올려 양쪽이 여기를 보게 했다.

⚠️ 임계값의 성격
---------------
아래 숫자들은 "네이버가 요구하는 기준"이 아니다. 업계에 도는 SEO 규칙
(글자 수 임계값, 감성 비율)은 네이버 공식 문서에 근거가 없다. 여기 값은
태권월드 기존 발행 글을 실측해서 나온, **기존 글과 스타일을 일치시키기
위한** 기준이다. 이 구분이 흐려지면 나중에 임계값을 조정할 때 판단
근거가 사라진다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Format(str, Enum):
    """초안 형식."""

    NAVER_BLOG = "naver_blog"
    MARKDOWN = "markdown"


@dataclass
class SeoConfig:
    """구조 임계값.

    기본값은 태권월드 기존 글 실측치에서 왔다.
    제목 20~22자, 본문 1,825~2,102자, 챕터 4개, 줄 길이 평균 12자.
    """

    format: Format = Format.NAVER_BLOG

    # 제목: 실측 20~22자. 업계 통설 '30~40자'는 네이버 공식 근거가 없어
    # 하한으로 삼지 않는다. 상한만 모바일 잘림을 고려해 남겨둔다.
    title_min: int = 15
    title_max: int = 40

    # 기존 글 실측 순수 본문 1,825~2,102자.
    # 파일 전체 문자 수(2,600~3,100)와 혼동하지 말 것. 네이버 블로그는
    # 줄바꿈이 많아 파일 크기와 본문 분량이 25% 가까이 벌어진다.
    body_min_chars: int = 1600
    body_max_chars: int = 2800

    min_chapters: int = 3

    # --- NAVER_BLOG 전용 ---
    max_line_chars: int = 20
    min_short_line_ratio: float = 0.90

    # --- MARKDOWN 전용 ---
    max_paragraph_chars: int = 500
    min_images: int = 3
    caption_min: int = 20
    caption_max: int = 50

    title_keyword_head: int = 15


@dataclass
class CheckConfig:
    """검사 임계값 설정.

    임계값을 넘으면 warnings에 문구가 추가되지만 passed를 뒤집지는 않는다.
    발행 차단은 오직 severity=block 인 금칙어에 의해서만 발생한다.
    감성사전은 도메인 편차가 커서 판정 근거로 쓰기엔 신뢰도가 부족하다.
    """

    # 단일 명사가 전체 명사에서 차지하는 비율의 상한.
    # 분모가 명사만이라 전체 토큰 기준보다 2.5~3배 높게 나온다.
    # 주제어는 정상적인 글에서도 5~6%에 도달하므로 8%를 기본값으로 둔다.
    max_keyword_density: float = 0.08
    min_lexical_diversity: float = 0.35  # 어휘 다양성 하한
    min_style_consistency: float = 0.90  # 문체 일관성 하한 (기존 글 실측 95~98%)
    max_negative_ratio: float = 0.35  # 부정 문장 비율 상한
    min_char_count: int = 800  # 본문 최소 길이
    max_avg_sentence_length: float = 90.0  # 문장 평균 길이 상한
    top_keywords: int = 15
    strip_markdown: bool = True
    exclude_boilerplate: bool = True  # 캡션·출처·저작권 문구를 분석에서 제외
    check_seo: bool = True  # 마크다운 구조 SEO 검사 수행 여부
    keyword_density_min_count: int = 3  # 밀도 경고를 낼 최소 출현 횟수
    # 명사가 이보다 적으면 밀도 경고를 내지 않는다. 표본이 작으면
    # 명사 1~2개 차이로 밀도가 크게 흔들려 지표로 쓸 수 없다.
    keyword_density_min_nouns: int = 50

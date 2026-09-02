"""초안·소주제 생성 관련 설정.

모델 체인, 분량 임계값, 요청 간격을 모은다.

여기 모은 기준
-------------
"어떻게 만드는가"가 아니라 "얼마나 / 몇 개 / 얼마 간격으로"에 해당하는 값.
프롬프트 문구와 응답 스키마는 설정이 아니라 산출물의 일부이므로
prompts/ 와 각 에이전트의 *_schema.py 에 둔다.

분량 값이 두 계통으로 나뉜 이유
------------------------------
초안 생성 에이전트가 둘이고 서로 다른 분량 정책을 쓴다.

  SCHEMA_*  schema_draft_agent (JSON 스키마 → 코드가 마크다운 조립)
            원문 길이 x LENGTH_RATIO 로 상한을 잡는다.
            MAX_LENGTH 2000 은 Notion rich_text 속성 1개의 하드 리밋이라
            임의로 올릴 수 없다.

  LENGTH_TIERS  freeform 계열 (draft_prompt)
            원문 정보량을 4구간으로 나눠 목표 구간을 준다.
            얇은 소재에 고정 분량을 요구하면 LLM이 빈칸을 지어낸다는
            실측(633자 단신 → 1,750자 글에서 늘어난 분량 대부분이
            수식어와 같은 사실의 반복)에서 나온 구조다.

둘을 통합할지는 data/quality/metrics.jsonl 측정 결과가 나온 뒤 결정한다.
"""

from __future__ import annotations

import os

# ── Gemini 모델 체인 (작업별 분리) ────────────────────────
# 'latest' 별칭을 쓰면 세대 교체 시 코드 수정 없이 최신 모델을 따라간다.
# 구세대(2.0/2.5 이하)는 무료 티어에서 정리되어 429/404가 나므로 쓰지 않는다.
# 사용 가능 모델 확인: python scripts/diagnose_gemini.py
#
# 무료 티어 실측 한도 (2026-07 기준, 계정마다 다를 수 있음):
#   Flash      : RPM 5,  RPD 20    ← 품질은 높지만 한도가 매우 빡빡
#   Flash Lite : RPM 15, RPD 500   ← 한도가 25배, 정형 작업에는 충분
#
# 소주제 생성 : 스키마로 출력이 고정된 정형 작업 → Lite 우선
# 블로그 작성 : 긴 원문을 읽고 장문을 쓰는 작업 → 문맥 파악·환각 억제 중요,
#              호출 수가 적어 Flash의 RPD 20으로 감당된다
# 삽화 사양   : 기사 1건당 (소제목 수 + 1)회를 부른다. 소주제 생성보다도
#              호출이 잦고 출력이 짧은 JSON 이라 Lite 로 충분하다
#
# 각 체인은 소진 시 상대 작업의 모델로도 넘어가도록 뒤쪽에 배치했다.

SUBTOPIC_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
SUBTOPIC_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
        "gemini-flash-latest,gemini-3.5-flash",
    ).split(",")
    if m.strip()
]

WRITER_MODEL = os.getenv("GEMINI_WRITER_MODEL", "gemini-flash-latest")
WRITER_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_WRITER_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-flash-lite-latest,"
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",")
    if m.strip()
]

# 삽화 사양(기사 분석·Visual Plan). 체인이 짧으면 Flash 한도가 찬 날
# 소제목 전부가 같은 폴백 Plan 으로 떨어져 같은 그림 4장이 나온다.
# 초안 작성이 Flash 를 다 쓴 뒤에도 남아 있도록 Lite 를 앞에 몰아 둔다.
ILLUST_MODEL = os.getenv("GEMINI_ILLUST_MODEL", "gemini-flash-lite-latest")
ILLUST_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_ILLUST_FALLBACK_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
        "gemini-flash-latest,gemini-3.5-flash",
    ).split(",")
    if m.strip()
]

# ── 소주제 생성 ──────────────────────────────────────────
# 소주제 개수는 원문 정보량에 맞춘다. 짧은 단신에서 4개를 뽑으면 서로 겹치고,
# 글도 같은 사실을 되풀이한다. (실측: 600자 단신 → 4개 → 섹션 1·2가 동일 내용)
SUBTOPIC_MIN = 3
SUBTOPIC_MAX = 4
SUBTOPIC_LONG_BODY = 1200  # 원문이 이 길이 이상이면 4개, 미만이면 3개
SUBTOPIC_PROMPT_BODY_LIMIT = 3000  # 프롬프트에 넣을 본문 상한 (자)
SUBTOPIC_REQUEST_INTERVAL = 5.0  # 요청 간 최소 간격(초). 무료 티어 RPM 여유

# 한 요청에 담을 기사 수. RPD는 '요청 건수' 기준이므로 3건을 묶으면
# RPD 소모가 1/3로 줄어든다. TPM(250K)은 여유가 커서 토큰은 제약이 아니다.
# 1로 두면 개별 호출과 동일하게 동작한다 (품질 문제 시 되돌리기 용).
SUBTOPIC_BATCH_SIZE = int(os.getenv("SUBTOPIC_BATCH_SIZE", "3"))

# ── 스키마 방식 초안 (schema_draft_agent) ──────────────────
SCHEMA_BODY_LIMIT = 6000  # 원문 전달 상한 (자)
SCHEMA_TARGET_LENGTH = 1800  # 분량 상한(자). MAX_LENGTH에 여유를 둔 값
SCHEMA_MAX_LENGTH = 2000  # Notion rich_text 속성 1개의 하드 리밋
SCHEMA_MIN_TARGET = 900  # 너무 짧아지지 않도록 하한
SCHEMA_REQUEST_INTERVAL = 6.0  # 글 생성 간 간격(초). 응답이 길어 여유를 둔다

# 원문 대비 최대 배수. 실측: 633자 단신 → 1,750자 글(2.8배)에서 늘어난 분량이
# 대부분 수식어와 같은 사실의 반복이었다. 1.8배면 도입·마무리·설명 보강 정도.
SCHEMA_LENGTH_RATIO = 1.8

# ── 자유 텍스트 초안 (draft_prompt) ───────────────────────
# (원문 최소 길이, 목표 하한, 목표 상한, 챕터 수 문자열)
LENGTH_TIERS = [
    (1200, 1900, 2300, "4~5"),
    (600, 1600, 2000, "3~4"),
    (250, 1300, 1700, "3"),
    (0, 1000, 1400, "3"),
]

# 이보다 원본이 짧으면 글을 쓰지 말고 사람이 판단하도록 알린다.
MIN_SOURCE_CHARS = 120

# 이 이상이면 기사 하나로 여러 챕터를 채울 수 있다 = 단독 발행 가능.
# LENGTH_TIERS 최상위 구간과 같은 값이다.
# article_grouper(묶음 판단)와 선정 단계(발행구분 표시)가 함께 참조하므로
# 모듈 상수가 아니라 여기에 둔다. 두 곳의 기준이 어긋나면 노션에 '단독'으로
# 표시된 기사가 publish 에서 묶음으로 처리되는 모순이 생긴다.
SOLO_THRESHOLD = 1200

# 과거 글 예시를 프롬프트에 넣을 때 발췌 길이
EXAMPLE_EXCERPT_CHARS = 1800

# ── LLM 타임아웃 배수 (REQUEST_TIMEOUT 곱) ────────────────
# 장문 생성은 소주제 추출보다 훨씬 오래 걸린다.
SUBTOPIC_TIMEOUT_MULTIPLIER = 6
DRAFT_TIMEOUT_MULTIPLIER = 18


# 삽화 종목 정책. taekwondo | article | hybrid
#   taekwondo — 기사 종목과 무관하게 항상 태권도 (태권월드 기본값)
#   article   — 기사 종목 그대로
#   hybrid    — 소제목에 태권도 언급이 있으면 태권도, 없으면 기사 종목.
#               마지막 1장은 태권도로 고정(앵커)
ILLUST_DOMAIN_MODE = os.getenv("ILLUST_DOMAIN_MODE", "taekwondo")

# hybrid 에서 마지막 삽화를 태권도로 고정할지
ILLUST_TAEKWONDO_ANCHOR = os.getenv("ILLUST_TAEKWONDO_ANCHOR", "1") == "1"
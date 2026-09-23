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

두 계통 모두 이 파일이 단일 출처다. 에이전트 쪽에 같은 이름의 상수를
두지 않는다. 예전에 draft_prompt 가 LENGTH_TIERS 사본을 들고 있었고,
상한을 1,900 으로 낮춘 튜닝이 사본에만 반영돼 이 파일에는 2,300 이
남아 있었다. 검사기(quality_gate)는 이 파일을, 생성기는 사본을 보는
상태였다. 같은 사고가 schema_draft_agent / subtopic_agent 에도 있었다.

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
# 소주제는 기사 1건당 1개다. 본문 길이와 무관하다(2026-09 단독형 폐지).
# 한 편은 기사 4건 × 챕터 1개로 채운다. 개수 상수는 subtopic_agent 가
# 갖는다(SUBTOPIC_PER_ARTICLE).
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
# 원본 정보량에 따른 목표 분량 구간.
# 소재가 얇은데 분량을 고정하면 LLM이 빈칸을 지어내 채운다.
#
# (원문 최소 길이, 목표 하한, 목표 상한, 챕터 수 문자열)
#
# 상한은 어느 구간에서도 BODY_MAX_CHARS(1,900)를 넘지 않는다.
# 처음에는 2,000자로 잡았는데, LLM이 목표 상한에 맞춰 쓰다 보니 매번
# 아슬아슬하게 넘겼다. (실측: 지시 2,000자 → 결과 2,031자, 품질검사 경고)
# 상한이 목표에 딱 붙어 있으면 넘칠 여지가 없으므로 100자를 비워 둔다.
#
# 챕터 수 문자열은 프롬프트 표기용이다. 실제 챕터 수는 article_grouper 가
# 4개로 고정하고 DraftBrief.chapter_count 가 정하므로, 구간마다 다른 값을
# 적어 두면 지시문과 실제가 어긋난다. 네 구간 모두 "4"로 통일한다.
LENGTH_TIERS = [
    (1200, 1700, 1900, "4"),
    (600, 1500, 1800, "4"),
    (250, 1300, 1600, "4"),
    (0, 1000, 1400, "4"),
]

# 목표 상한의 절대 한계. 구간 표를 손댈 때 실수로 넘기지 않도록 둔다.
# 이 값이 프롬프트 지시와 품질 검사(SeoConfig.body_max_chars) 양쪽에
# 함께 들어가야 "1,300자로 쓰세요" 시킨 뒤 "1,600자 미만입니다" 경고하는
# 모순이 생기지 않는다.
BODY_MAX_CHARS = 1900

# 글의 고정 부분(도입부·마무리·자주 묻는 질문)이 차지하는 대략의 분량.
# draft_prompt 가 챕터당 분량을 지시할 때 전체 목표에서 먼저 뺀다.
#
# 2026-09-23 가이드 예시로 어림한 값이다 (도입 약 200 · 마무리 약 170 ·
# FAQ 약 240 · 챕터 소제목 약 80). 실제 초안에서 재서 고친다.
# 너무 작게 잡으면 글이 넘치고, 너무 크게 잡으면 챕터가 얇아진다.
#   실측(2026-09-23): 전체 목표를 그대로 챕터 수로 나눠 지시했더니 모델이
#   챕터를 지키고 고정 부분을 더해 순수 본문 평균 2,351자(목표 1,700~1,900).
FIXED_PARTS_CHARS = 650

# 이보다 원본이 짧으면 글을 쓰지 말고 사람이 판단하도록 알린다.
MIN_SOURCE_CHARS = 120

# 과거 글 예시를 프롬프트에 넣을 때 발췌 길이
EXAMPLE_EXCERPT_CHARS = 1800

# ── 삽화 종목 정책 ────────────────────────────────────────
# 삽화 종목 정책. taekwondo | article | hybrid
#   taekwondo — 기사 종목과 무관하게 항상 태권도 (태권월드 기본값)
#   article   — 기사 종목 그대로
#   hybrid    — 소제목에 태권도 언급이 있으면 태권도, 없으면 기사 종목.
#               마지막 1장은 태권도로 고정(앵커)
ILLUST_DOMAIN_MODE = os.getenv("ILLUST_DOMAIN_MODE", "taekwondo")

# hybrid 에서 마지막 삽화를 태권도로 고정할지
ILLUST_TAEKWONDO_ANCHOR = os.getenv("ILLUST_TAEKWONDO_ANCHOR", "1") == "1"
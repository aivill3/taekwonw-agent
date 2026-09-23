"""수집·랭킹 파라미터.

'무엇을 얼마나 가져올지'와 '어떻게 고를지'의 기준값.
수집기(tools/)와 랭킹 에이전트(agents/ranking/)가 함께 본다.

단일 출처는 LANES 다
------------------
'어떤 키워드를 어느 카테고리로 묶을지'는 LANES 가 혼자 정한다.
SEARCH_KEYWORDS(전체 검색어)와 LANE_NAMES(Notion select 옵션)는 둘 다
LANES 에서 파생한다. 같은 사실을 두 군데에 적으면 반드시 어긋난다 —
2026-09 에 Notion '검색키워드' 옵션은 원시 키워드로 만들어지는데 저장값은
레인 이름이라, 쓰이지도 않는 옵션만 보드에 쌓였다.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# ── 수집 파라미터 ────────────────────────────────────────
# 네이버 정렬. sim=관련도순(네이버 뉴스 검색 화면의 기본 정렬), date=최신순.
# 검색 화면에 보이는 순서를 따르기 위해 sim 을 쓴다. 관련도순은 며칠 지난
# 기사도 상위에 올리지만, 날짜 필터(LOOKBACK_DAYS)와 processed_urls 가 거른다.
NAVER_SORT = os.getenv("NAVER_SORT", "sim")

FIRST_RUN_BACKFILL_DAYS = 2  # 첫 실행(기준 시각 없음) 시 거슬러 올라갈 최대 일수

# 수집 대상 기간(일). '직전 실행 이후'만 보면 창이 너무 좁아,
# 사건이 며칠에 걸쳐 보도되는 경우(관련 기사가 늦게 나오는 경우)를 놓친다.
# 이 값만큼 거슬러 올라가 다시 훑되, 이미 처리한 기사는 processed_urls 가 걸러낸다.
#
# 레인이 조회 일수를 지정하면 그쪽이 이긴다. 여기 값은 생략한 자리의 기본값이다.
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))

# 한 번 실행할 때 레인 1개당 쓸 글 수. 한 편 = 기사 4건.
# 한 실행의 LLM 호출: 소주제 ≈ (레인 수 × 편수 × 4) / SUBTOPIC_BATCH_SIZE,
# 초안 = 레인 수 × 편수. 하루 두 번 돌므로 Gemini RPD 20 안에서 정한다.
#
# 이름이 '키워드당'인 것은 레인 도입 전의 잔재다. collect.yml 과 .env 에
# 같은 이름으로 들어가 있어, 바꾸려면 양쪽을 함께 고쳐야 한다.
POSTS_PER_KEYWORD = int(os.getenv("POSTS_PER_KEYWORD", "1"))

# 키워드 1개당 가져올 기사 수 (API 상한 100).
# 관련도순은 오래된 기사가 섞여, 날짜 필터에서 빠지는 비율이 최신순보다 높다.
# 신규 건수가 모자라면 이 값을 올린다.
#
# 레인이 수집 건수를 지정하면 그쪽이 이긴다. 여기 값은 생략한 자리의 기본값이다.
NAVER_DISPLAY = int(os.getenv("NAVER_DISPLAY", "30"))


# ── 레인(발행 카테고리) ──────────────────────────────────
# 레인 = 블로그 카테고리 하나. 키워드 여러 개를 한 레인에 묶을 수 있고,
# 레인마다 수집 건수와 조회 일수를 따로 준다.
#
#   형식: "이름:키워드,키워드:수집건수:조회일수 | 다음 레인..."
#   예  : "조직:국기원,태권도협회:100:5 | 태권도:태권도:50:3"
#
# 수집 건수·조회 일수는 생략할 수 있고, 생략하면 NAVER_DISPLAY / LOOKBACK_DAYS 를 쓴다.
#
# 적은 순서가 선정 우선순위다. 앞 레인이 고른 사건은 뒤 레인 후보에서 빠지므로,
# 뉴스가 적은 레인을 앞에 둔다. 조직 뉴스는 하루 1~2개 사건이라 뒤에 두면 굶는다.
# (2026-09-18: 태권도를 앞에 뒀더니 조직 레인이 후보 19건 중 8건을 잃고 0편)
#
# 레인마다 값을 달리 두는 이유:
#   수집 건수 — 조직 뉴스는 건수가 적어 한 건이 순위 밖으로 밀리는 손실이 크다.
#               (2026-09-18: 같은 검색어를 몇 분 간격으로 불렀더니 12위 기사가
#                50위 밖으로 밀렸다. 네이버 관련도 순위는 호출마다 달라진다)
#               다만 후보 전체의 본문을 선정 전에 받는다(_extract_once).
#               이 값을 올리면 추출 시간이 그대로 따라 늘어난다.
#   조회 일수 — 조직 뉴스는 하루 단위로 쏟아지지 않는다. 창을 넓게 잡아야
#               4건 묶음을 채운다. 대회·행사 뉴스는 신선도가 중요해 3일로 둔다.
#
# ⚠️ 로컬 .env 와 collect.yml 에 같은 LANES 를 둘 것. 한쪽에만 있으면 로컬
#    dry-run 과 CI 가 다른 구조로 돌아, 튜닝 결과를 믿을 수 없게 된다.

_DISPLAY_MAX = 100  # 네이버 API 상한


@dataclass(frozen=True)
class Lane:
    """발행 카테고리 하나. 수집부터 묶기까지 이 단위로 돈다."""

    name: str
    # tuple 인 이유: frozen dataclass 는 __hash__ 를 만드는데, 여기에 list 를
    # 담으면 set 이나 dict 키로 쓰는 순간 unhashable 로 터진다.
    keywords: tuple[str, ...]
    display: int
    lookback_days: int


def _parse_lanes(spec: str) -> list[Lane]:
    """레인 스펙 문자열 → Lane 목록. 하나도 못 읽으면 예외를 던진다.

    숫자 자리가 깨져도 레인 자체는 살린다. 기본값으로 돌리고 경고만 남긴다 —
    오타 하나로 그 카테고리의 글이 통째로 안 나가는 편이 더 나쁘다.

    반대로 레인이 0개가 되는 것은 통과시키면 안 된다. 워크플로의
    `for lane in LANES` 가 0회 돌아 '새로운 기사가 없습니다' 로 정상 종료하므로,
    설정 실수가 평범한 빈 회차와 구분되지 않는다.
    """
    lanes: list[Lane] = []
    for chunk in spec.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        name = parts[0]
        if not name:
            continue
        keywords = (
            [k.strip() for k in parts[1].split(",") if k.strip()]
            if len(parts) > 1 and parts[1]
            else [name]
        )

        def _num(idx: int, default: int, label: str, _name=name, _parts=parts) -> int:
            # import 시점이라 로거가 아직 없다. stderr 로 쓴다.
            if len(_parts) <= idx or not _parts[idx]:
                return default
            try:
                return int(_parts[idx])
            except ValueError:
                print(
                    f"[collect_config] 레인 '{_name}' 의 {label} '{_parts[idx]}' 을 "
                    f"숫자로 읽지 못해 {default} 를 씁니다",
                    file=sys.stderr,
                )
                return default

        display = min(max(_num(2, NAVER_DISPLAY, "수집건수"), 1), _DISPLAY_MAX)
        lookback = max(_num(3, LOOKBACK_DAYS, "조회일수"), 1)
        lanes.append(Lane(name, tuple(keywords), display, lookback))

    if not lanes:
        raise ValueError(
            f"레인을 하나도 읽지 못했습니다 (LANES={spec!r}). "
            "형식: '이름:키워드,키워드:수집건수:조회일수 | 다음 레인'"
        )
    return lanes


# LANES 가 없으면 SEARCH_KEYWORDS 를 키워드 1개짜리 레인으로 풀어 쓴다.
# 예전 설정(collect.yml 에 SEARCH_KEYWORDS 만 있는 상태)에서도 그대로 돈다.
#
# ⚠️ 이 대비책은 키워드 1개 = 레인 1개 = 블로그 카테고리 1개로 풀린다.
#    여기에 '국기원,태권도협회' 를 적으면 카테고리가 그 수만큼 쪼개진다.
#    키워드를 묶을 자리는 LANES 다.
_ENV_KEYWORDS = [
    k.strip() for k in os.getenv("SEARCH_KEYWORDS", "태권도").split(",") if k.strip()
]
_DEFAULT_LANES = " | ".join(f"{k}:{k}" for k in _ENV_KEYWORDS)

LANES = _parse_lanes(os.getenv("LANES", _DEFAULT_LANES))

# 레인 이름 목록. Notion '검색키워드' select 옵션이 이것을 쓴다.
# 저장값(bundle["search_keyword"] = lane.name)과 출처가 같아야 옵션이 어긋나지 않는다.
LANE_NAMES = [lane.name for lane in LANES]

# 전체 검색 키워드. naver_news_client.collect_all() 을 인자 없이 부르는
# 수동 실행·테스트용 기본값이다. 워크플로는 항상 lane.keywords 를 명시해 넘긴다.
SEARCH_KEYWORDS = [k for lane in LANES for k in lane.keywords]
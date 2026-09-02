"""수집·랭킹 파라미터.

'무엇을 얼마나 가져올지'와 '어떻게 고를지'의 기준값.
수집기(tools/)와 랭킹 에이전트(agents/ranking/)가 함께 본다.
"""

from __future__ import annotations

import os

# ── 수집 파라미터 ────────────────────────────────────────
QUERY = "태권도"
FIRST_RUN_BACKFILL_DAYS = 2  # 첫 실행(기준 시각 없음) 시 거슬러 올라갈 최대 일수

# 수집 대상 기간(일). '직전 실행 이후'만 보면 창이 너무 좁아,
# 사건이 며칠에 걸쳐 보도되는 경우(관련 기사가 늦게 나오는 경우)를 놓친다.
# 이 값만큼 거슬러 올라가 다시 훑되, 이미 처리한 기사는 processed_urls 가 걸러낸다.
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "3"))
NAVER_DISPLAY = 30          # 네이버 API 1회 호출당 기사 수 (최대 100)
GOOGLE_RSS_URL = (
    f"https://news.google.com/rss/search?q={QUERY}&hl=ko&gl=KR&ceid=KR:ko"
)

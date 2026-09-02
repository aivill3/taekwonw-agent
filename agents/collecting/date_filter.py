"""발행일 기준 필터 (두 소스 공통).

'이전 수집일 이후' 방식:
  state.last_collected_at 보다 나중에 발행된 기사만 통과시킨다.
  고정 시간창(24시간 등)과 달리 실행 간격이 불규칙해도 누락·중복이 없다.
    - 오래 안 돌렸으면 그만큼 넓게 가져오고
    - 하루에 여러 번 돌리면 각 실행이 직전 이후 구간만 가져온다

경계 처리:
  같은 시각(published == 기준)에 발행된 기사는 '통과'시킨다.
  잘라내면 경계에 걸친 기사를 영구히 놓치기 때문이고,
  중복 저장은 processed_urls 2차 필터가 막는다.

첫 실행:
  기준 시각이 없으면 FIRST_RUN_BACKFILL_DAYS 만큼만 거슬러 올라간다.
  (무제한이면 RSS가 주는 만큼 과거 기사가 전부 들어와 백필이 과도해진다)
"""
from datetime import datetime, timedelta

from config.settings import KST
from config.collect_config import FIRST_RUN_BACKFILL_DAYS, LOOKBACK_DAYS
from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)


def parse_dt(value: str) -> datetime | None:
    """저장된 발행일(ISO 8601, KST) 문자열 → datetime. 실패 시 None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 시간대 정보가 없는 값(예: trafilatura 메타데이터의 '2026-07-30')은 KST로 간주
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt


def resolve_cutoff(last_collected_at: str | None) -> datetime:
    """필터 기준 시각 결정.

    '직전 실행 이후'만 보면 창이 너무 좁다. 같은 사건이 며칠에 걸쳐 보도되거나
    (관련 기사가 하루 뒤에 나오는 경우) 첫 수집에서 본문 추출에 실패한 기사를
    영영 놓치게 된다. 그래서 LOOKBACK_DAYS 만큼은 항상 거슬러 올라가 다시 훑는다.
    이미 처리한 기사는 processed_urls 2차 필터가 걸러내므로 중복 저장은 없다.
    """
    now = datetime.now(KST)
    lookback = now - timedelta(days=LOOKBACK_DAYS)

    dt = parse_dt(last_collected_at) if last_collected_at else None
    if dt:
        # 직전 실행 시각과 lookback 중 '더 이른' 쪽을 기준으로 (= 더 넓게 본다)
        cutoff = min(dt, lookback)
        if cutoff < dt:
            log.info(f"조회 창 확대: 최근 {LOOKBACK_DAYS}일치를 다시 훑습니다")
        return cutoff

    cutoff = now - timedelta(days=FIRST_RUN_BACKFILL_DAYS)
    log.info(f"첫 실행: 최근 {FIRST_RUN_BACKFILL_DAYS}일 이내 기사만 수집합니다")
    return cutoff


def filter_since(articles: list[Article], last_collected_at: str | None) -> list[Article]:
    """기준 시각 이후 발행분만 남긴다. 발행일 파싱 실패 건은 보수적으로 통과시킨다."""
    cutoff = resolve_cutoff(last_collected_at)

    kept, dropped, unknown = [], 0, 0
    for a in articles:
        dt = parse_dt(a.published)
        if dt is None:
            unknown += 1  # 날짜 불명은 버리지 않는다 (신규 기사를 놓치는 편이 더 나쁨)
            kept.append(a)
        elif dt >= cutoff:
            kept.append(a)
        else:
            dropped += 1

    log.info(
        f"날짜 필터({cutoff:%Y-%m-%d %H:%M} 이후): {len(kept)}건 통과, "
        f"{dropped}건 제외" + (f", {unknown}건 날짜 불명(통과)" if unknown else "")
    )
    return kept


def latest_published(articles: list[Article]) -> str | None:
    """수집분 중 가장 늦은 발행 시각 (다음 실행의 기준값).

    ※ '지금 시각'을 기준으로 저장하면 안 된다. 언론사 발행과 RSS 노출 사이에
      지연이 있어, 지금 시각으로 잘라버리면 그 사이에 나온 기사를 영구히 놓친다.
      실제로 수집한 기사의 최신 발행일을 기준으로 삼아야 안전하다.
    """
    times = [parse_dt(a.published) for a in articles]
    valid = [t for t in times if t]
    return max(valid).isoformat() if valid else None
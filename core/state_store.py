"""실행 간 상태 저장 (state/state.json).

담는 것은 두 가지다.
  last_collected_at  마지막으로 수집한 기사의 발행 시각
  processed_urls     이미 Notion 에 저장한 기사 URL

'지금 시각'이 아니라 '마지막 기사의 발행 시각'을 남기는 이유:
  RSS 는 기사가 나오고 한참 뒤에 항목을 노출한다. 실행 시각을 기준으로 잘라
  다음 실행에서 그 이후만 보면, 노출이 늦은 기사는 영영 못 가져온다.
  date_filter.resolve_cutoff() 가 이 값과 LOOKBACK_DAYS 중 이른 쪽을 택한다.

processed_urls 를 따로 두는 이유:
  날짜 필터는 '언제 발행됐나'만 본다. 같은 기사가 며칠에 걸쳐 RSS 에 계속
  뜨는 경우가 흔해서, 발행일만으로는 매번 다시 수집된다. Notion 저장에
  성공한 URL 만 기록하므로, 저장이 실패한 기사는 다음 실행에서 다시 시도된다.

URL 은 collect_workflow 가 정규화(공백·끝 슬래시 제거)해서 넘긴다.
여기서 또 손대면 두 곳의 규칙이 어긋날 때 원인을 찾기 어렵다.

왜 data/ 가 아니라 state/ 인가:
  GitHub Actions 는 실행마다 새 컨테이너다. 러너가 갱신한 파일은
  커밋되지 않으면 컨테이너와 함께 사라진다. 워크플로들은 실행 뒤
  `git add state/` 로 상태를 남기므로, 그 밖에 있으면 갱신분이 버려진다.

    실측(2026-09-14): data/state.json 의 마지막 커밋이 사람이 만든
    것뿐이었다. Actions 수집이 며칠째 같은 지점에서 다시 시작하고 있었고,
    날짜 필터와 중복 제거가 매번 초기 상태로 돌아 이미 처리한 기사를
    다시 긁고 있었다(Notion 의 skip_duplicates 가 막아 결과물은 멀쩡해
    드러나지 않았다).

  stock_usage.json · model_state.json 과 같은 자리에 둔다. 성격이 같은
  파일이 흩어져 있으면 워크플로마다 add 대상을 빠뜨릴 자리가 생긴다.
"""
import json
from datetime import datetime

from config.settings import KST, STATE_DIR
from core.logger import get_logger

log = get_logger(__name__)

STATE_PATH = STATE_DIR / "state.json"

# 보관할 URL 개수 상한. 무한히 쌓이면 파일이 커지고 매 실행 로딩이 느려진다.
# 하루 100건 안팎이므로 이 값이면 두 달 넘게 커버한다.
MAX_PROCESSED_URLS = 5000


def _empty() -> dict:
    return {"last_collected_at": None, "processed_urls": []}


def load() -> dict:
    """이전 실행 상태. 파일이 없거나 깨졌으면 빈 상태로 시작한다.

    깨진 파일에 멈추지 않는 이유: 상태가 없으면 첫 실행처럼
    FIRST_RUN_BACKFILL_DAYS 만큼 훑으면 그만이다. 중복은 Notion 쪽
    skip_duplicates 가 걸러 준다. 파이프라인을 세울 만한 사유가 아니다.
    """
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("이전 상태 없음 — 첫 실행으로 진행합니다")
        return _empty()
    except (OSError, ValueError) as e:
        log.warning(f"상태 파일을 읽지 못해 초기화합니다 ({e})")
        return _empty()

    if not isinstance(data, dict):
        log.warning("상태 파일 형식이 올바르지 않아 초기화합니다")
        return _empty()

    state = _empty()
    state["last_collected_at"] = data.get("last_collected_at") or None
    urls = data.get("processed_urls")
    state["processed_urls"] = [u for u in urls if isinstance(u, str)] if isinstance(urls, list) else []
    return state


def save(state: dict) -> None:
    """상태를 파일에 쓴다. 실패해도 예외를 올리지 않는다.

    여기서 멈추면 이미 끝난 수집·저장이 헛일이 된다. 다음 실행이 기사 몇 건을
    중복 처리하는 편이 낫다.
    """
    payload = {
        "last_collected_at": state.get("last_collected_at"),
        "processed_urls": state.get("processed_urls", [])[-MAX_PROCESSED_URLS:],
        "updated": datetime.now(KST).isoformat(timespec="seconds"),
    }
    try:
        STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning(f"상태 저장 실패: {e}")


def mark_processed(
    state: dict, urls: list[str], collected_until: str | None = None
) -> dict:
    """처리 완료 URL 을 더하고 수집 기준 시각을 갱신한 뒤 저장한다.

    collected_until 이 기존 값보다 과거면 무시한다. 발행일이 뒤죽박죽인 기사가
    섞였을 때 기준이 뒤로 밀리면, 이미 처리한 구간을 다시 훑게 된다.
    """
    known = set(state.get("processed_urls", []))
    added = [u for u in urls if u and u not in known]
    state["processed_urls"] = state.get("processed_urls", []) + added

    if collected_until:
        previous = state.get("last_collected_at")
        if not previous or collected_until > previous:
            state["last_collected_at"] = collected_until
        else:
            log.info(
                f"수집 기준 시각 유지 ({previous}) — 이번 최신값({collected_until})이 더 과거"
            )

    save(state)
    log.info(
        f"상태 갱신: 처리 URL {len(added)}건 추가 "
        f"(누적 {len(state['processed_urls'])}건, 기준 {state.get('last_collected_at')})"
    )
    return state
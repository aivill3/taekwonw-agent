"""모델 한도 상태를 실행 간에 유지한다.

무료 티어의 일일 요청 한도(RPD)는 모델마다 따로 소진된다. 그런데 실행할 때마다
체인 맨 앞부터 다시 시도하면, 이미 한도가 끝난 모델에 매번 네 번씩 요청을 던지고
90초를 기다린 뒤에야 폴백으로 넘어간다.
(실측 2026-08-14: 같은 실패를 09:15 · 10:35 · 11:09 세 번의 실행에서 반복.
 그 과정에서 다른 모델의 한도까지 함께 갉아먹었다)

그래서 '오늘 한도가 끝난 모델'을 파일에 남겨 다음 실행이 건너뛰게 한다.
소주제 생성과 초안 작성이 서로 다른 체인을 쓰지만 상태는 공유한다 —
한쪽에서 소진된 모델은 다른 쪽에서도 못 쓰기 때문이다.

리셋 시점 — KST 자정이 아니라 '태평양 시간 자정'이다:
  구글 무료 티어의 RPD는 미국 태평양 시간 자정에 초기화된다. 한국 시간으로는
  서머타임 기간(3~11월) 오후 4시, 그 외 오후 5시다.
  KST 자정을 기준으로 삼으면 아침 실행이 아직 풀리지 않은 한도를 다시 들이받아,
  고치려던 문제가 그대로 남는다.

저장 위치 — state/ 이지 data/ 가 아니다:
  data/ 는 .gitignore 로 빠진다. GitHub Actions 는 실행마다 새 컨테이너라
  거기 두면 기록이 매번 사라지고, 한도가 끝난 모델에 실행마다 다시
  들이받는다. 고치려던 문제가 클라우드에서 그대로 재현되는 셈이다.
  state/ 는 커밋 대상이라 워크플로가 푸시하면 다음 실행이 이어받는다.

기록을 지우고 처음부터 다시 시도하려면 state/model_state.json 을 삭제한다.
"""
import json
from datetime import datetime, timedelta, timezone

from config.settings import DATA_DIR, KST, STATE_DIR
from core.logger import get_logger

log = get_logger(__name__)

STATE_PATH = STATE_DIR / "model_state.json"

# 예전 위치. 한 번만 옮긴다.
_LEGACY_PATH = DATA_DIR / "model_state.json"


def _migrate_legacy() -> None:
    """data/ 에 있던 기록을 state/ 로 한 번 옮긴다.

    옮기지 않고 그냥 새로 시작해도 하루면 복구되지만, 그 하루 동안
    이미 소진된 모델에 다시 요청을 던진다.
    """
    if STATE_PATH.exists() or not _LEGACY_PATH.exists():
        return
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            _LEGACY_PATH.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _LEGACY_PATH.unlink()
        log.info(f"모델 상태를 {STATE_PATH.name} 로 옮겼습니다")
    except OSError as e:
        log.warning(f"모델 상태 이전 실패(무시하고 진행): {e}")


_migrate_legacy()

try:
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:
    # Windows 는 IANA 시간대 DB를 기본 제공하지 않는다 (pip install tzdata 필요).
    # 없으면 PST(UTC-8) 고정으로 둔다. 서머타임 기간에 리셋을 한 시간 늦게
    # 인식할 뿐이라, 너무 일찍 리셋해 429를 다시 맞는 것보다 안전하다.
    _PACIFIC = timezone(timedelta(hours=-8))


def quota_day() -> str:
    """한도 기준일(태평양 시간). 이 값이 바뀌면 기록을 통째로 버린다."""
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("quota_day") != quota_day():
        return {}  # 날짜가 바뀌었다 → 전부 초기화 (체인 맨 앞부터 다시)
    return data


def exhausted_models() -> set[str]:
    """오늘 한도가 소진된 것으로 기록된 모델."""
    return set(_load().get("exhausted", []))


def mark_exhausted(model: str, reason: str = "") -> None:
    """모델을 오늘치 소진 목록에 넣는다. 저장 실패는 경고만 남긴다."""
    models = exhausted_models()
    if model in models:
        return
    models.add(model)
    payload = {
        "quota_day": quota_day(),
        "exhausted": sorted(models),
        "updated": datetime.now(KST).isoformat(timespec="seconds"),
        "last_reason": reason[:200],
    }
    try:
        STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info(f"모델 '{model}' 오늘치 소진으로 기록 (다음 실행부터 건너뜀)")
    except OSError as e:
        log.warning(f"모델 상태 저장 실패: {e}")


def usable_chain(chain: list[str]) -> list[str]:
    """체인에서 오늘 한도가 끝난 모델을 걸러낸다.

    전부 걸리면 원본을 그대로 돌려준다. 기록이 틀렸을 가능성(한도가 이미 풀렸는데
    날짜 경계를 잘못 잡았다든지)을 남겨두는 편이, 아무것도 시도하지 않고
    빈손으로 끝나는 것보다 낫다.
    """
    dead = exhausted_models()
    if not dead:
        return chain

    usable = [m for m in chain if m not in dead]
    skipped = [m for m in chain if m in dead]
    if not usable:
        log.warning(
            f"기록상 체인의 모든 모델({len(chain)}개)이 소진 상태입니다. "
            f"그래도 한 번 시도합니다"
        )
        return chain

    log.info(f"오늘 한도 소진으로 건너뜀: {', '.join(skipped)}")
    return usable
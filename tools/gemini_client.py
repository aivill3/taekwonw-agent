"""Gemini 공통 유틸 — 엔드포인트, 모델 체인, 429 판정, 재시도 대기.

LLM을 부르는 세 곳(소주제 · 초안 · 삽화 개념)이 같은 실패 양상을 만나므로
판정 로직을 한곳에 둔다. 각 에이전트는 요청 본문과 응답 파싱만 책임진다.

429를 세 갈래로 나누는 이유:
  같은 429라도 대응이 정반대다.
    RPM 초과   → 잠깐 기다리면 풀린다.        재시도한다
    RPD 소진   → 오늘은 끝났다.               폴백 모델로 넘어간다
    할당량 0   → 이 키로는 아예 못 쓴다.      폴백 모델로 넘어간다
  구분하지 않고 재시도하면, 이미 끝난 한도에 대고 몇 분을 기다린 뒤에야
  폴백으로 넘어간다. 그 사이 다른 모델의 한도까지 시간이 흐른다.

판정은 응답 본문의 violations[].quotaId 문자열을 본다. 구글이 이 필드에
'GenerateRequestsPerDayPerProjectPerModel-FreeTier' 처럼 무엇을 어겼는지
적어 주기 때문이다. 상태 코드만으로는 셋을 구분할 수 없다.
"""
import json
import os
import random
import re

from core.logger import get_logger

log = get_logger(__name__)

# API 버전은 settings 가 아니라 여기서 읽는다. Gemini 엔드포인트 형식은
# 이 모듈만의 관심사이고, config/ 는 프로젝트 전역 튜닝 값만 담기 때문이다.
API_VERSION = os.getenv("GEMINI_API_VERSION", "v1beta")
API_BASE = f"https://generativelanguage.googleapis.com/{API_VERSION}/models"

MAX_RETRIES = 3
# 재시도할 5xx. 4xx 는 다시 던져도 결과가 같으므로 넣지 않는다.
RETRYABLE_STATUS = {500, 502, 503, 504}
SERVER_BACKOFF_BASE = 5.0  # 5xx 재시도 기본 대기(초): 5 → 10 → 20

# RPM(분당 요청) 초과 시의 기본 대기. 구글이 retryDelay 를 주면 그 값을 쓴다.
RPM_BACKOFF_BASE = 20.0
MAX_WAIT = 90.0  # 한 번에 이보다 오래 기다리지 않는다. 그럴 바엔 폴백이 낫다.

# quotaId 안에서 각 상황을 가리키는 조각
_DAILY_MARKERS = ("PerDay", "PerProjectPerDay")
_MINUTE_MARKERS = ("PerMinute",)

# 'retryDelay': '32s' 형태를 뽑는다 (JSON 파싱이 실패해도 원문에서 건진다)
_RE_RETRY_DELAY = re.compile(r'"?retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s', re.I)


class ModelExhausted(Exception):
    """이 모델로는 더 진행할 수 없다. 호출부는 폴백 모델로 전환해야 한다.

    '요청이 실패했다'와는 다르다. 실패는 재시도하면 되지만 이쪽은 재시도가
    의미 없으므로, 호출부가 두 경우를 섞지 않도록 예외로 구분한다.
    """


def api_url(model: str = "") -> str:
    """generateContent 엔드포인트. 모델을 비우면 베이스 URL만 돌려준다."""
    if not model:
        return API_BASE
    return f"{API_BASE}/{model}:generateContent"


def build_chain(primary: str, fallbacks: list[str] | None = None) -> list[str]:
    """주 모델 + 폴백들로 시도 순서를 만든다. 중복은 앞선 것만 남긴다.

    같은 모델이 두 번 들어가면 이미 한도가 끝난 모델을 또 시도하게 된다.
    """
    chain: list[str] = []
    for m in [primary, *(fallbacks or [])]:
        m = (m or "").strip()
        if m and m not in chain:
            chain.append(m)
    if not chain:
        raise ValueError("모델 체인이 비어 있습니다. draft_config 를 확인하세요.")
    return chain


def _as_dict(body) -> dict:
    """응답 본문을 dict 로. 문자열이면 파싱해 보고, 실패하면 빈 dict."""
    if isinstance(body, dict):
        return body
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _violations(body) -> list[dict]:
    """error.details 안의 QuotaFailure violations 목록."""
    data = _as_dict(body)
    details = data.get("error", {}).get("details", []) or []
    out: list[dict] = []
    for d in details:
        if not isinstance(d, dict):
            continue
        if "QuotaFailure" in str(d.get("@type", "")):
            out.extend(v for v in d.get("violations", []) if isinstance(v, dict))
    return out


def violated_quota_ids(body) -> list[str]:
    """어떤 할당량을 어겼는지. 로그에 그대로 남겨 원인을 눈으로 확인한다."""
    return [str(v.get("quotaId", "")) for v in _violations(body) if v.get("quotaId")]


def is_daily_quota(body) -> bool:
    """일일 한도(RPD) 소진인가. 참이면 오늘 이 모델은 못 쓴다."""
    ids = violated_quota_ids(body)
    return any(any(m in qid for m in _DAILY_MARKERS) for qid in ids)


def is_minute_quota(body) -> bool:
    """분당 한도(RPM) 초과인가. 참이면 잠시 뒤 재시도하면 된다."""
    ids = violated_quota_ids(body)
    return any(any(m in qid for m in _MINUTE_MARKERS) for qid in ids)


def looks_like_zero_quota(body) -> bool:
    """이 키·모델 조합에 할당량이 아예 배정돼 있지 않은가.

    무료 티어에서 지원되지 않는 모델을 부르면 한도가 0인 채로 429가 온다.
    이 경우 기다리는 것은 완전한 낭비다 — 내일도 같은 결과다.
    구글은 quotaValue 를 0 으로 주거나 값을 아예 빼고 보낸다.
    """
    violations = _violations(body)
    if not violations:
        return False
    for v in violations:
        raw = v.get("quotaValue", None)
        if raw is None:
            continue
        try:
            if float(raw) <= 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def wait_seconds(body, attempt: int = 0) -> float:
    """다음 재시도까지 기다릴 초.

    구글이 retryDelay 를 주면 그 값을 존중한다 — 서버가 아는 회복 시점이
    이쪽 추측보다 정확하다. 없으면 지수 백오프에 지터를 얹는다.
    지터가 없으면 여러 요청이 같은 순간에 재시도해 초과를 재현한다.
    """
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    m = _RE_RETRY_DELAY.search(text or "")
    if m:
        try:
            return min(float(m.group(1)) + 1.0, MAX_WAIT)
        except ValueError:
            pass
    wait = RPM_BACKOFF_BASE * (2 ** attempt) * random.uniform(0.8, 1.2)
    return min(wait, MAX_WAIT)

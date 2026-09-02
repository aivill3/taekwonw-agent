"""삽화 경로가 공유하는 모델 폴백 체인.

왜 별도 모듈인가:
  article_analyzer 와 visual_planner 는 모델명 하나만 받아 쓰다가, 그 모델의
  일일 한도가 차면 ModelExhausted 를 그대로 위로 던졌다. 그러면 파이프라인이
  통째로 멈춘다 — 삽화 하나 때문에 초안 발행까지 실패 통지가 나간다.

  초안 작성 경로(schema_draft_agent)에는 이미 폴백 체인이 있는데, v3 삽화
  경로를 새로 만들면서 그 장치가 빠졌다. 여기서 채운다.

한 번 소진된 모델은 프로세스가 사는 동안 기억한다. 기사 1건에 소제목이
4개면 Plan 을 4번 만드는데, 매번 소진된 모델부터 다시 시도하면 429 를
네 번 더 받는다. 그만큼 시간이 늘고 서버에도 부담이다.
"""
from __future__ import annotations

from core.logger import get_logger
from tools.gemini_client import ModelExhausted

log = get_logger(__name__)

_exhausted: set[str] = set()


def chain(primary: str, fallbacks: list[str]) -> list[str]:
    """시도할 모델 순서. 이미 소진된 것은 뺀다.

    중복도 제거한다. primary 가 fallbacks 에도 있으면 두 번 부르게 된다.
    """
    ordered = [m for m in [primary, *fallbacks] if m]
    seen, out = set(), []
    for m in ordered:
        if m in seen or m in _exhausted:
            continue
        seen.add(m)
        out.append(m)
    return out


def mark(model: str) -> None:
    """이 모델은 오늘 더 못 쓴다고 기록한다."""
    if model not in _exhausted:
        _exhausted.add(model)
        log.warning(f"모델 한도 소진 — 이후 건너뜁니다: {model}")


def call(fn, primary: str, fallbacks: list[str]):
    """fn(model) 을 체인 순서로 시도한다.

    fn 은 성공 시 결과를, 실패 시 None 을 돌려주고, 한도 소진이면
    ModelExhausted 를 던지는 함수여야 한다.

    모두 실패하면 None. 호출부는 폴백 Plan 으로 넘어가면 된다 —
    여기서 예외를 던지면 폴백 장치가 있어도 도달하지 못한다.
    """
    models = chain(primary, fallbacks)
    if not models:
        log.warning("쓸 수 있는 모델이 없습니다 (전부 한도 소진)")
        return None

    for model in models:
        try:
            result = fn(model)
        except ModelExhausted:
            mark(model)
            continue
        if result is not None:
            return result
        # None 은 일시적 실패다. 다음 모델로 넘어가 본다.
        log.info(f"  {model} 응답 없음 — 다음 모델 시도")

    return None
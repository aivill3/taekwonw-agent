"""5단계 — SDXL 프롬프트 조립 (Prompt Compilation).

프롬프트를 만드는 곳은 여기 하나다.
planned_image_agent 에서 `_hard_filter` 와 `_render` 를 가져온다.
반대 방향은 함수 안에서 지연 import 한다 (최상단에서 서로 import 하면 순환).

--- 값의 근거 ---------------------------------------------------------

아래 상수는 전부 GPU 서버에 직접 쏴서 6장씩 뽑아 고른 것이다.
추측이 아니라 실측이다. 세 번의 측정에서 이렇게 움직였다.

    네거티브 24개 (해부학 용어 포함)   인체 온전 4/6 · 사용 가능 1/6
    네거티브 16개 (해부학 용어 제거)   인체 온전 5/6 · 사용 가능 4/6

1) 해부학 용어를 네거티브에 넣으면 역효과다  ★
   'three legs, extra limbs, deformed hands, malformed feet, twisted neck'
   를 빼자 오히려 사지가 안정됐다. 넣었을 때 다리 3개가 나왔다.
   막고 싶은 것을 이름으로 부르면 모델이 그 개념을 활성화한다.

2) 공중 동작을 금지하면 앉은 자세로 도망간다
   'jumping, airborne, mid-air, floating' 을 넣자 무릎 꿇거나 주저앉은
   그림이 절반이었다. 실제로 막아야 할 것은 'sitting, kneeling, lying down'
   쪽이다. 이 셋으로 바꾸니 서 있는 동작이 안정적으로 나온다.

3) 부정문은 긍정 토큰으로 읽힌다
   'not a karate gi' 는 CLIP 이 not 을 무시하고 karate gi 를 활성화한다.
   도복 앞섶이 겹쳐 여민 형태로 나왔다. 부정은 전부 네거티브로만 쓴다.

4) 동작은 동사가 아니라 신체 배치로 써야 한다
   'performing a roundhouse kick' 은 정자세로 수렴한다.
   'one leg extended horizontally at shoulder height, supporting foot
   planted' 처럼 관절 위치를 쓰면 그대로 그려진다.

5) 77 토큰에서 잘린다 — 그리고 잘려도 되는 것이 있다
   검증에 쓴 프롬프트는 91 토큰이라 끝의 'shallow depth of field,
   sharp focus' 가 모델에 닿지 않았다. 그런데도 결과가 좋았다.
   즉 스타일 문구는 피사체·동작·도복보다 덜 중요하다. 우선순위를
   그렇게 잡는다 — 넘치면 스타일 뒤쪽부터 버린다.
"""
from __future__ import annotations

import inspect
import re

from core.logger import get_logger
from agents.illustration.planned_image_agent import _hard_filter, _render

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# 네거티브 — 짧게. 해부학 용어를 넣지 않는다(위 1번).
# ─────────────────────────────────────────────────────────────

NEGATIVE = (
    "karate gi, cross-over lapel, judo, "
    "logos, text, watermark, "
    "3d render, cgi, illustration, cartoon, blurry"
)

# 인물이 있을 때만. 'sitting, kneeling, lying down' 이 핵심이다 —
# 서 있는 동작을 지키는 역할을 이 셋이 한다.
NEGATIVE_PERSON = (
    "two people, second person, crowd, "
    "sitting, kneeling, lying down, "
    "shoes, sneakers, socks, "
    "celebrity, recognizable public figure"
)

# 인물이 없을 때만.
NEGATIVE_NO_PERSON = "people, human figure, crowd"

# 인물이 있는 자리에서 네거티브에 들어가면 안 되는 말.
# 앞의 두 묶음은 이유가 다르다.
#   - 신체 부위: 그리라고 해놓고 지우라고 하면 뭉개진다
#   - 해부학 오류 용어: 실측에서 역효과였다(다리 3개가 나왔다)
PERSON_CONFLICT = {
    # 신체 부위
    "people", "person", "human", "humans", "faces", "face",
    "hands", "hand", "fingers", "feet", "bare feet", "legs",
    "full body", "human figure", "figures", "athlete", "athletes",
    # 해부학 오류 용어
    "three legs", "extra limbs", "extra arms", "extra legs",
    "missing limb", "duplicated body", "twisted neck",
    "deformed hands", "malformed feet", "distorted face",
    "mutated hands", "bad anatomy",
    # 공중 동작을 막으면 앉은 자세로 도망간다
    "jumping", "airborne", "mid-air", "floating",
}

# ─────────────────────────────────────────────────────────────
# 프롬프트 조각 — 실측 토큰 수를 주석에 남긴다.
# 합이 72를 넘으면 뒤가 잘리므로 함부로 늘리면 안 된다.
# ─────────────────────────────────────────────────────────────

# 10토큰. 원래 'sharp focus' 가 더 붙어 있었으나 매번 잘려나갔다.
STYLE = "telephoto sports photograph, shallow depth of field"

# 16토큰. 부정문('not a karate gi')을 넣지 않는다 — 네거티브가 담당한다.
UNIFORM = "white V-neck dobok with black collar and belt, bare feet"

# 13토큰. 'dojang' 은 일본식 도장 이미지를 부른다(쇼지·다다미가 나왔다).
# 'training hall' + 벽·바닥 재질을 직접 쓰는 편이 안정적이다.
SETTING_PERSON = "empty training hall with plain white wall and wooden floor"

# 12토큰. 인물이 없는 정물 장면용.
SETTING_OBJECT = "in a taekwondo dojang, wooden floor, natural window light"

# 익명화. 실존 인물 소제목에서 특정인으로 읽히는 것을 막는다.
# 네거티브의 celebrity / recognizable public figure 와 짝이다.
ANON_PERSON = "an anonymous ordinary teenager"

TOKEN_BUDGET = 72


def _approx_tokens(text: str) -> int:
    """CLIP BPE 토큰 수 근사.

    영어 산문은 단어당 약 1.3 토큰이고 구두점이 각각 1 토큰이다.
    정확한 값은 tokenizer 를 불러야 알 수 있지만 판정에는 충분하다.
    """
    words = len(re.findall(r"[A-Za-z0-9']+", text))
    puncts = len(re.findall(r"[,.:;()\-]", text))
    return round(words * 1.3) + puncts


def _clean(text: str) -> str:
    """문장 끝 마침표를 뗀다. 뒤에 콤마가 붙으면 토큰이 깨진다."""
    return text.strip().rstrip(".").strip()


def compile_prompt(plan, identifiers: list[str]) -> str:
    """Visual Plan → SDXL positive 프롬프트.

    조각을 우선순위 순으로 쌓고, 예산을 넘으면 낮은 우선순위부터 버린다.

    우선순위 (실측 근거는 모듈 docstring 5번)
      1 피사체·동작 (scene)   없으면 그림이 성립하지 않는다
      2 도복                  종목 정합의 핵심. 없으면 가라테로 샌다
      3 익명화 / 무인 문구     초상 안전에 직결된다
      4 배경                  있으면 좋지만 scene 에 이미 들어 있기도 하다
      5 스타일                잘려도 결과가 유지됐다(실측)
      6 조명(mood)
      7 카메라 기술어          가장 먼저 버린다
    """
    scene = _clean(plan.scene)
    if not scene:
        return ""

    has_person = getattr(plan, "subject_count", 0) > 0
    is_tkd = getattr(plan, "domain", "taekwondo") == "taekwondo"
    low = scene.lower()

    parts: list[tuple[int, str]] = [(1, scene)]

    if is_tkd:
        if has_person:
            parts.append((2, UNIFORM))
            if "hall" not in low and "wall" not in low and "floor" not in low:
                parts.append((4, SETTING_PERSON))
        elif not any(w in low for w in ("taekwondo", "dobok", "dojang")):
            parts.append((2, SETTING_OBJECT))

    parts.append((3, ANON_PERSON if has_person else "no people in the frame"))
    parts.append((5, STYLE))
    if getattr(plan, "mood", ""):
        parts.append((6, _clean(plan.mood)))
    parts.append((7, f"{plan.shot} shot"))

    dropped: list[str] = []
    while True:
        text = ", ".join(t for _, t in parts)
        if _approx_tokens(text) <= TOKEN_BUDGET or len(parts) <= 2:
            break
        worst = max(p for p, _ in parts)
        dropped.extend(t for p, t in parts if p == worst)
        parts = [(p, t) for p, t in parts if p != worst]

    if dropped:
        log.info(f"  토큰 예산 초과({_approx_tokens(text)}) — 제외: {' / '.join(dropped)}")

    # 격리한 고유명사가 scene 으로 새어 들어왔을 수 있다. 마지막에 한 번 더.
    text, removed = _hard_filter(text, identifiers)
    if removed:
        log.warning(
            f"  하드 필터: 고유명사 {len(removed)}개 제거 — {', '.join(removed[:4])}"
        )
    return text


def compile_negative(plan, extra: list[str] | None = None) -> str:
    """Visual Plan → SDXL negative 프롬프트.

    plan.forbidden 을 그대로 이어붙이지 않는다. Gemini 가 해부학 용어나
    신체 부위를 금지어로 넣는 일이 잦은데, 실측에서 그것들이 오히려
    결과를 망가뜨렸다(모듈 docstring 1·2번).
    """
    has_person = getattr(plan, "subject_count", 0) > 0

    parts = [p.strip() for p in NEGATIVE.split(",") if p.strip()]
    tail = NEGATIVE_PERSON if has_person else NEGATIVE_NO_PERSON
    parts += [p.strip() for p in tail.split(",") if p.strip()]
    seen = {p.lower() for p in parts}

    skipped: list[str] = []
    for term in list(getattr(plan, "forbidden", [])) + list(extra or []):
        t = term.strip()
        if not t or t.lower() in seen:
            continue
        if has_person and t.lower() in PERSON_CONFLICT:
            skipped.append(t)
            continue
        parts.append(t)
        seen.add(t.lower())

    if skipped:
        log.info(f"  네거티브에서 제외(역효과 확인된 항목): {', '.join(skipped)}")

    text = ", ".join(parts)

    # 앞쪽(종목 혼동·인원·자세)이 더 중요하므로 뒤에서 잘라낸다.
    while _approx_tokens(text) > TOKEN_BUDGET and len(parts) > 10:
        parts.pop()
        text = ", ".join(parts)
    return text


_render_takes_negative: bool | None = None


def render(plan, prompt: str, negative: str, out):
    """_render 를 부른다. negative 를 받는 시그니처면 함께 넘긴다.

    받지 않는 구버전이면 계산한 네거티브가 버려진다. 조용히 넘기지 않고
    한 번 경고한다 — 실제로 그 상태로 한동안 돌아갔다.
    """
    global _render_takes_negative

    if _render_takes_negative is None:
        try:
            _render_takes_negative = "negative" in inspect.signature(_render).parameters
        except (TypeError, ValueError):
            _render_takes_negative = False
        if not _render_takes_negative:
            log.warning(
                "_render 가 negative 인자를 받지 않습니다 — 계산한 네거티브가 "
                "생성기에 전달되지 않습니다. planned_image_agent 를 최신판으로 "
                "교체하세요."
            )

    if _render_takes_negative:
        return _render(plan, prompt, out, negative=negative)
    return _render(plan, prompt, out)

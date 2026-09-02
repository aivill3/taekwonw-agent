"""2단계 — Visual Plan 생성 (Visual Planning).

기사 해석을 '만들 그림의 사양'으로 바꾼다. 3단계 라이브러리 조회 조건과
5단계 프롬프트가 모두 여기서 나온다 — **단일 진실 원천**이다.

종횡비는 반드시 이 시점에 확정한다. 생성 후 크롭하면 구도가 깨진다.

--- 이 판에서 정리된 것 -------------------------------------------------

1) 종목이 사라지던 문제
   고유명사를 격리하면 남는 것이 category 라벨 하나뿐이었다. e스포츠
   기사에서 33개를 격리했더니 'event' 만 남았고, 2단계는 그것만 보고
   시상대와 태권도 띠를 계획했다.
   → analyzer 가 domain / occasion / setting_en 을 함께 넘긴다.

2) 태권도 블로그인데 다른 종목 그림이 나오던 문제
   ILLUST_DOMAIN_MODE 로 정책을 고른다. 기본값은 'taekwondo' —
   태권월드는 태권도 매체이므로 기사 종목이 무엇이든 삽화는 태권도로 간다.

3) 초상 문제를 '사람 제거'로 풀던 문제
   사람을 전부 빼니 빈 무대·빈 책상만 남았다. 지금은 사람을 그리되
   **누구도 아닌 얼굴**로 그린다. 실존 인물 소제목에는 익명화 지시가
   프롬프트에 사전 주입된다 — 사후에 깎지 않는다. 사후 교정은 인물
   중심으로 짜인 장면에서 사람만 빠진 모순을 남긴다.

4) 소제목이 무시되던 문제
   "느슨하게만 연결되면 충분합니다" 라는 문장이 있었고, 안전 사물 목록을
   주니 모델이 소제목을 보지 않고 그 목록에서 골랐다. 그 문장을 지우고
   heading_focus 를 받아 로그에 남긴다.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field, asdict

import requests

from config.settings import GEMINI_API_KEY, REQUEST_TIMEOUT
from config.draft_config import (
    ILLUST_FALLBACK_MODELS,
    ILLUST_DOMAIN_MODE,
    ILLUST_TAEKWONDO_ANCHOR,
)
from core.logger import get_logger
from tools.gemini_client import (
    MAX_RETRIES,
    RETRYABLE_STATUS,
    SERVER_BACKOFF_BASE,
    ModelExhausted,
    api_url,
    is_daily_quota,
    looks_like_zero_quota,
    wait_seconds,
)
from agents.illustration.article_analyzer import ArticleAnalysis, OCCASION_LABELS
from agents.illustration import model_chain

log = get_logger(__name__)

COMPOSITIONS = (
    "eye_level_side", "eye_level_front", "low_angle", "high_angle",
    "close_up", "wide_establishing",
)
SHOTS = ("close", "medium", "wide")
ASPECT_RATIOS = ("16:9", "4:3", "1:1", "3:4")

# 모든 Plan 에 공통으로 들어가는 금지 요소.
#
# 'faces' 와 'hands' 는 넣지 않는다. 사람을 그리라고 해놓고 얼굴과 손을
# 지우라고 하면 뭉개진 인물이 나온다. 특정인으로 읽히는 것을 막는 일은
# 익명화 문구(ANON_PERSON_RULE + 프롬프트 조립부)가 담당한다.
BASE_FORBIDDEN = [
    "text", "letters", "numbers", "logos", "watermark",
    "banner", "scoreboard", "event emblem", "year markings",
    "engraved numerals", "jersey numbers",
    # 실사풍 유지. 헤드셋 삽화가 3D 렌더처럼 나온 적이 있다.
    "3d render", "cgi", "illustration", "cartoon", "digital art",
]

# 종목별 '안정적으로 나오는' 인물 없는 소재.
SAFE_OBJECTS = {
    "taekwondo": (
        "빈 도장의 마루, 매트, 벽에 걸린 색 띠, 트로피, 격파용 송판, "
        "창으로 든 빛, 정렬된 보호구, 옷걸이에 걸린 도복"
    ),
    "other_martial_art": (
        "빈 수련장 마루, 매트, 벽에 걸린 보호구, 트로피, 정돈된 도복"
    ),
    "other_sport": (
        "빈 관중석, 경기장 조명, 라커룸 벤치, 옷걸이에 걸린 유니폼, 트로피"
    ),
    "esports": (
        "빈 경기용 부스와 게이밍 의자, 책상 위의 키보드와 마우스, 헤드셋, "
        "무대 조명, 관객석을 향한 큰 화면(글자 없이 단색)"
    ),
    "non_sport": (
        "빈 연단과 마이크, 회의장 좌석, 서류 폴더가 놓인 긴 탁자"
    ),
}

# 행사 성격에 맞는 태권도 동작. 소제목의 뜻을 태권도 장면으로 옮기는
# 다리 역할을 한다. 이것이 없으면 '출정식' 이 그냥 빈 무대가 된다.
#
# **동사가 아니라 신체 배치로 쓴다.** 실측 결과다.
#   'performing a roundhouse kick' → 정자세로 수렴한다. 동사구는 CLIP 이
#   약하게 처리한다.
#   'one leg extended horizontally at shoulder height, supporting foot
#   planted' → 그대로 그려진다. 관절 위치는 명사구라 강하게 반영된다.
#
# 각 항목은 **18토큰 이하**여야 한다. 5단계에서 도복·배경·스타일과 합쳐지고
# CLIP 은 77토큰에서 자른다. 늘리면 스타일 문구가 통째로 사라진다.
# 실측 예산: SUBJECT 8 + UNIFORM 16 + SETTING 13 + STYLE 10 = 47,
# 남는 것이 25 이므로 동작은 18 이내가 안전하다.
TAEKWONDO_ACTIONS = {
    "send_off": "standing at attention, arms straight down, head bowed forward",
    "award": "standing upright on a plain podium, arms at sides, chin lifted",
    "agreement": "two people facing each other, right arms extended in a handshake",
    "opening": "standing still facing away, arms at sides, seen from behind",
    "press": "seated at a table, hands resting flat, facing forward",
    "competition": "one leg extended horizontally at shoulder height, supporting foot planted",
    "training": "one knee raised high, foot snapping toward a handheld paddle",
    "none": "one leg extended horizontally at shoulder height, supporting foot planted",
}

# category 기준 동작. occasion 이 'none' 일 때 이쪽을 먼저 본다.
# 겨루기·품새·시범은 서로 완전히 다른 자세다.
TAEKWONDO_ACTIONS_BY_CATEGORY = {
    "sparring": "side-facing guard stance, both fists closed at chest",
    "poomsae": "deep front stance, one arm blocking at head height, fist at waist",
    "demonstration": "descending elbow strike over a stacked wooden board, torso leaning forward",
    "interview": "seated at a table, hands resting flat, facing forward",
}


def _action_for(analysis) -> str:
    """이 기사에 맞는 신체 배치 서술.

    occasion 이 구체적이면 그것을, 아니면 category 를, 둘 다 아니면 기본값.
    """
    if analysis.occasion != "none":
        hit = TAEKWONDO_ACTIONS.get(analysis.occasion)
        if hit:
            return hit
    return (TAEKWONDO_ACTIONS_BY_CATEGORY.get(analysis.category)
            or TAEKWONDO_ACTIONS["none"])

OCCASION_HINTS = {
    "send_off": "대회에 나가기 **전**입니다. 메달·시상대·트로피는 아직 등장하지 "
                "않습니다. 준비하고 떠나는 장면입니다.",
    "award": "대회가 끝난 **뒤**입니다. 시상대, 메달, 트로피가 자연스럽습니다.",
    "agreement": "실내 서명 자리입니다. 경기장·시상대는 맞지 않습니다.",
    "opening": "시작을 알리는 자리입니다.",
    "press": "기자회견 자리입니다. 마이크와 탁자가 맞습니다.",
    "competition": "경기가 진행 중입니다.",
    "training": "훈련·수련 장면입니다.",
    "none": "",
}

# 소제목에 이 말이 있으면 기사 종목과 무관하게 태권도로 그린다.
TAEKWONDO_WORDS = (
    "태권도", "도복", "품새", "겨루기", "국기원", "도장", "발차기",
    "격파", "단증", "사범", "관장", "태권",
)

# 사람을 가리키는 말. 실명이 없어도 독자는 기사에 나온 그 사람으로 읽는다.
PERSON_WORDS = (
    "선수", "감독", "코치", "회장", "위원장", "관장", "사범", "심판",
    "대표팀", "선수단", "국가대표", "지도자", "챔피언",
)

# scene 에 종목 표지가 없으면 생성 모델이 학습 데이터에서 가장 흔한 종목으로
# 채운다. 'a personal locker in a team dressing room' 이 축구 라커룸으로
# 나온 것이 그 경우다.
DOMAIN_MARKERS = {
    "taekwondo": ("taekwondo", "dobok", "dojang", "martial art", "sparring",
                  "training hall", "belt", "kick", "board break", "mat"),
    "other_martial_art": ("martial art", "dojo", "mat", "belt", "training hall"),
    "other_sport": ("stadium", "pitch", "court", "track", "field", "arena"),
    "esports": ("esports", "gaming", "computer", "keyboard", "headset",
                "monitor", "console", "booth"),
    "non_sport": ("hall", "podium", "table", "office", "meeting"),
}

CLOSE_HINT = re.compile(r"\bclose[- ]?up\b", re.I)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "heading_focus": {"type": "string"},
        "scene": {"type": "string"},
        "subject_count": {"type": "integer"},
        "composition": {"type": "string", "enum": list(COMPOSITIONS)},
        "shot": {"type": "string", "enum": list(SHOTS)},
        "mood": {"type": "string"},
        "aspect_ratio": {"type": "string", "enum": list(ASPECT_RATIOS)},
        "forbidden": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["heading_focus", "scene", "subject_count", "composition",
                 "shot", "mood", "aspect_ratio", "forbidden"],
}

ANON_PERSON_RULE = """## 이 소제목의 인물은 익명으로 그립니다

소제목이 기사의 실존 인물을 가리킵니다({reason}).
**그 사람을 그리는 것이 아니라, 같은 상황에 있는 이름 없는 수련생**을 그립니다.

- scene 에 이름·소속·등번호를 암시하는 요소를 넣지 않습니다
- **shot 은 medium 을 씁니다.** 얼굴만 크게 잡는 인물 사진(close)은
  특정인의 초상처럼 읽힙니다
- **동작이 있어야 합니다.** 정지한 얼굴보다 발차기·품새·띠 매기 같은
  행동이 익명성에도 유리하고 지면에서도 낫습니다"""

DOMAIN_OVERRIDE_NOTE = """## 이 삽화는 태권도 소재로 만듭니다

사유: {why}

기사 종목이 무엇이든 **이 자리의 장면은 태권도**로 잡습니다.
위 '장면 배경'에 적힌 종목 묘사는 이 삽화에 한해 무시하세요.
소제목의 정서(준비·도전·성취·회고 등)를 태권도의 공간과 동작으로 옮깁니다.

이 자리에 어울리는 신체 배치: {actions}
배경은 도장 또는 실내 수련장으로 고정합니다."""

# 동작을 어떻게 써야 하는지. 이 지시가 없으면 Gemini 가 동사구를 쓰고,
# 그러면 그림이 정자세로 수렴한다. 실측으로 확인한 규칙이다.
ACTION_RULE = """## 매우 중요: 동작은 동사가 아니라 신체 배치로 씁니다

그림 모델은 동사구를 약하게 처리합니다. 'performing a roundhouse kick'
이라고 쓰면 발차기가 아니라 가만히 선 자세가 나옵니다. 실측입니다.

**관절과 팔다리가 어디에 있는지**를 쓰세요.

| 이렇게 쓰지 마세요 | 이렇게 쓰세요 |
|---|---|
| performing a high kick | one leg extended horizontally at shoulder height, supporting foot planted |
| practicing poomsae | deep front stance, one arm blocking at head height, fist at waist |
| breaking a board | descending elbow strike over a stacked wooden board, torso leaning forward |
| standing in formation | standing at attention, arms straight down, head bowed forward |

참고할 신체 배치: {actions}

그리고 **소리·감정처럼 그릴 수 없는 서술은 넣지 마세요.**
'radiating confidence', 'laughter echoing' 같은 표현은 화면에 나타나지
않으면서 자리만 차지합니다."""

PROMPT = """당신은 스포츠 매체의 아트 디렉터입니다.
아래 소제목 자리에 넣을 사진의 **사양**을 정해 주세요.

## 이 삽화가 들어갈 자리의 소제목

{heading}

## 기사 맥락

종목·영역: {domain}
행사 성격: {occasion}
{occasion_hint}
장면 배경(영어): {setting_en}
핵심 장면: {summary}
소재 키워드: {keywords}

{domain_note}

{person_rule}

{action_rule}

## 매우 중요: 소제목과의 정합성

**소제목이 가리키는 상황·행위가 장면에 드러나야 합니다.**
사람 없는 장면을 고르더라도, 그 사물이 왜 이 소제목 자리에 놓이는지
설명될 수 있어야 합니다.

- 소제목이 '출정식'인데 시상대를 그리면 실패입니다 (대회 전후가 뒤바뀝니다)
- 소제목이 '태권도'인데 다른 종목 장비를 그리면 실패입니다

**heading_focus** 에 소제목의 어느 요소를 장면으로 옮겼는지 영어 2~5단어로
적으세요. 적을 말이 없으면 장면을 다시 고르세요.

{used_block}

## 매우 중요: 일반화

특정 대회·장소·인물을 알아볼 수 있는 요소는 넣지 않습니다.
**단, 종목과 행사 성격은 일반화 대상이 아닙니다. 반드시 남깁니다.**

| 이렇게 쓰지 마세요 | 이렇게 쓰세요 |
|---|---|
| LED wall showing "WORLD TAEKWONDO GP" | large LED wall with abstract light panels, no text |
| KPNP chest guards, dobok marked "KOREA" | unbranded electronic chest protectors, plain white dobok |
| 무주 태권도원 / 제17회 총장배 | generic indoor competition arena |

## 매우 중요: 생성 모델의 한계

- **작게 들어간 얼굴** — 화면에 사람이 셋 이상이면 전부 뭉개집니다
- **손과 발** — 전신이 다 보이는 구도에서 덩어리로 뭉개집니다
- **글자** — 화면·현수막·메달에 글자를 넣으면 깨진 문자가 나옵니다

그래서 이렇게 정해 주세요.

1. **subject_count 는 0 또는 1입니다.** 2 이상을 쓰지 않습니다.

2. 사람 없는 장면(0)도 좋은 선택입니다. 아래 소재가 안정적으로 나옵니다.
   다만 **소제목과 무관한 사물을 고르지는 마세요.**

   {safe_objects}

3. 사람을 넣는다면(1) **shot 은 medium 을 우선**합니다. wide 를 쓰지 않습니다.
   전신이 다 나오지 않도록 상반신에서 무릎 위 정도로 잡습니다.

4. **composition 의 wide_establishing 은 subject_count 가 0일 때만** 씁니다.

## 각 항목

- **heading_focus**: 소제목의 어느 요소를 장면으로 옮겼는지 영어 2~5단어
- **scene**: 무슨 장면인지 영어로 **한 문장**. 인물의 동작과 배경을 담습니다.
  **종목을 알 수 있는 단어가 반드시 들어가야 합니다.**
  'a personal locker in a team dressing room' 은 실패입니다 — 종목이 없어
  그림 모델이 임의로 축구로 그립니다.
  **동작은 신체 배치로 씁니다**(위 규칙 참조).
  소리·감정처럼 그릴 수 없는 서술은 넣지 마세요(웃음소리, 따뜻한 분위기 등).
  **25단어를 넘기지 마세요.** 5단계에서 도복·배경·스타일이 덧붙는데
  그림 모델은 77토큰에서 자릅니다. 길면 스타일 지시가 통째로 사라집니다.
- **subject_count**: 화면에 나오는 사람 수. **0 또는 1만 씁니다.**
- **composition**: {compositions} 중 하나
- **shot**: {shots} 중 하나
- **mood**: 실제 카메라로 찍은 사진의 조명과 색감을 영어로 짧게 씁니다.
  자연광·실내 조명·창가 역광처럼 **현실에 존재하는 광원**을 씁니다.
  neon glow, volumetric god rays, cinematic teal-orange 같은 렌더 조명
  용어는 쓰지 마세요 — 그림이 3D 렌더처럼 나옵니다.
- **aspect_ratio**: {ratios} 중 하나. 블로그 본문 삽화는 보통 16:9 또는 4:3.
- **forbidden**: 이 그림에 나오면 안 되는 것들.
  **사람을 그리는 자리에서는 people / faces / hands 를 넣지 마세요** —
  얼굴과 손이 뭉개집니다.

JSON 으로만 응답하세요."""


@dataclass
class VisualPlan:
    scene: str = ""
    subject_count: int = 1
    composition: str = "eye_level_side"
    shot: str = "medium"
    mood: str = "neutral indoor lighting"
    aspect_ratio: str = "16:9"
    forbidden: list[str] = field(default_factory=lambda: list(BASE_FORBIDDEN))
    heading: str = ""
    heading_focus: str = ""
    category: str = "other"
    domain: str = "taekwondo"
    anonymous: bool = False   # 익명 인물 자리인가 (프롬프트 조립부가 참조)

    def to_dict(self) -> dict:
        return asdict(self)

    def as_query(self) -> str:
        """3단계 CLIP 유사도 정렬에 쓸 텍스트."""
        return f"{self.scene}. {self.mood}. {self.shot} shot, {self.composition}."


# 폴백은 소제목마다 다른 값이 나와야 한다. 하나만 두면 실패가 겹칠 때
# 같은 그림이 여러 장 들어간다.
FALLBACK_TAEKWONDO = {
    "send_off": [
        "folded white dobok and a colored belt on a bench in an empty taekwondo dojang",
        "a pair of training shoes and a gym bag by the door of a taekwondo dojang",
        "an empty taekwondo dojang with wooden floor and morning light from windows",
    ],
    "award": [
        "a medal with a ribbon resting on a folded white dobok, soft indoor light",
        "an empty award podium beside a taekwondo competition mat",
    ],
    "press": [
        "a microphone on a table in front of a plain wall, a folded dobok beside it",
    ],
    "competition": [
        "an empty octagonal taekwondo competition mat with blue and red zones",
        "unbranded electronic chest protectors laid out beside a competition mat",
    ],
    "training": [
        "a stack of plain wooden boards on the floor of an empty taekwondo dojang",
        "kicking paddles leaning against the wall of a taekwondo training hall",
    ],
    "none": [
        "an empty taekwondo dojang, wooden floor, natural light from tall windows",
        "colored taekwondo belts rolled and lined up on a training mat",
        "a folded white dobok on a wooden bench in a quiet training hall",
    ],
}

FALLBACK_BY_DOMAIN = {
    "esports": [
        ("an empty esports stage with gaming chairs and desks, keyboards and "
         "headsets, blank screens, stage lighting"),
        "a headset and mechanical keyboard on a competition desk, cool lighting",
    ],
    "other_sport": [
        "empty stadium seating under indoor floodlights, no signage",
        "a locker room bench with folded towels and a water bottle",
    ],
    "non_sport": [
        "an empty indoor hall with a plain podium, microphone and rows of seats",
    ],
}


def _fallback(
    analysis: ArticleAnalysis, heading: str, variant: int = 0,
    domain: str = "taekwondo",
) -> VisualPlan:
    """LLM 을 못 쓸 때의 최소 Plan. 전부 인물이 없다.

    폴백은 여러 소제목에 걸릴 수 있으므로 variant 로 장면을 돌린다.
    """
    if domain == "taekwondo":
        pool = FALLBACK_TAEKWONDO.get(analysis.occasion) or FALLBACK_TAEKWONDO["none"]
    else:
        pool = FALLBACK_BY_DOMAIN.get(domain) or FALLBACK_TAEKWONDO["none"]

    scene = pool[variant % len(pool)]
    close = bool(CLOSE_HINT.search(scene))
    return VisualPlan(
        scene=scene,
        subject_count=0,
        composition="close_up" if close else "wide_establishing",
        shot="close" if close else "wide",
        heading=heading,
        heading_focus="(fallback)",
        category=analysis.category,
        domain=domain,
    )


def _person_risk(analysis: ArticleAnalysis, heading: str) -> str | None:
    """이 자리의 인물을 익명으로 그려야 하는가. 사유를 돌려준다.

    두 갈래로 본다.
      1) 소제목에 실명이 그대로 있는 경우
      2) 기사에 실명 인물이 있고, 소제목이 사람을 가리키는 말을 쓰는 경우

    2)가 필요한 이유는 '베테랑 선수들의 메달 도전' 같은 소제목 때문이다.
    이름이 없어도 독자는 기사에 나온 그 사람들로 읽는다.

    사람을 못 그리게 하는 것이 아니라, **누구도 아닌 얼굴**로 그리게 한다.
    """
    for name in analysis.identifiers.people:
        n = name.strip()
        if len(n) >= 2 and n in heading:
            return f"'{n}' 지목"
    if analysis.identifiers.people:
        for w in PERSON_WORDS:
            if w in heading:
                return f"실명 인물 기사 + 소제목의 '{w}'"
    return None


def _has_domain_marker(scene: str, domain: str) -> bool:
    s = scene.lower()
    return any(m in s for m in DOMAIN_MARKERS.get(domain, ()))


def _effective_domain(
    analysis: ArticleAnalysis, heading: str, index: int, total: int
) -> tuple[str, str]:
    """이 삽화 자리에 쓸 종목과 그 사유.

    태권도 블로그이므로 기사 종목을 그대로 따르는 것이 항상 옳지는 않다.
    소제목이 태권도를 말하는데 e스포츠 장비를 그리면 그것도 어긋난다.
    """
    if ILLUST_DOMAIN_MODE == "taekwondo":
        return "taekwondo", "블로그 고정 모드"
    if ILLUST_DOMAIN_MODE == "article":
        return analysis.domain, ""
    # hybrid
    if any(w in heading for w in TAEKWONDO_WORDS):
        return "taekwondo", "소제목에 태권도 언급"
    if (ILLUST_TAEKWONDO_ANCHOR and index == total
            and analysis.domain != "taekwondo"):
        return "taekwondo", "블로그 정체성 앵커(마지막 1장)"
    return analysis.domain, ""


def _call_gemini(payload: dict, model: str) -> dict | None:
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={"x-goog-api-key": GEMINI_API_KEY,
                         "Content-Type": "application/json"},
                json=payload,
                timeout=REQUEST_TIMEOUT * 6,
            )
            if resp.status_code == 429:
                body = resp.json() if resp.content else {}
                if looks_like_zero_quota(body) or is_daily_quota(body):
                    raise ModelExhausted("일일 한도 소진")
                if attempt < MAX_RETRIES:
                    time.sleep(wait_seconds(resp.text, attempt))
                    continue
                return None
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                time.sleep(SERVER_BACKOFF_BASE * (2 ** attempt))
                continue
            if not resp.ok:
                log.warning(f"Visual Plan 응답 오류 [{resp.status_code}]")
                return None
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"```(?:json)?", "", text).strip())
        except ModelExhausted:
            raise
        except Exception as e:
            log.warning(f"Visual Plan 생성 실패: {e}")
            return None
    return None


def plan_for(
    analysis: ArticleAnalysis,
    heading: str,
    model: str,
    used_scenes: list[str] | None = None,
    variant: int = 0,
    index: int = 1,
    total: int = 1,
) -> VisualPlan:
    """소제목 하나에 대한 Visual Plan.

    used_scenes 는 같은 기사에서 이미 확정된 장면들이다. 프롬프트에 넣어
    중복을 피한다. 사후에 걸러내면 대체할 장면이 없어 폴백으로 떨어진다.
    """
    eff_domain, why = _effective_domain(analysis, heading, index, total)
    domain_key = eff_domain if eff_domain in SAFE_OBJECTS else "taekwondo"

    if not GEMINI_API_KEY:
        return _fallback(analysis, heading, variant, eff_domain)

    reason = _person_risk(analysis, heading)
    if why:
        log.info(f"  종목 → 태권도 ({why})")
    if reason:
        log.info(f"  익명 인물로 계획합니다 — {reason}")

    used_block = ""
    if used_scenes:
        listed = "\n".join(f"- {s}" for s in used_scenes)
        used_block = (
            "## 이미 이 글에 쓴 장면 (반복 금지)\n\n"
            f"{listed}\n\n"
            "위와 같은 장소·사물·동작을 다시 쓰지 마세요."
        )

    domain_note = ""
    if why:
        domain_note = DOMAIN_OVERRIDE_NOTE.format(
            why=why,
            actions=_action_for(analysis),
        )

    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(
            compositions=" / ".join(COMPOSITIONS),
            shots=" / ".join(SHOTS),
            ratios=" / ".join(ASPECT_RATIOS),
            domain=domain_key,
            occasion=OCCASION_LABELS.get(analysis.occasion, "해당 없음"),
            occasion_hint=OCCASION_HINTS.get(analysis.occasion, ""),
            setting_en=analysis.setting_en or "(없음)",
            summary=analysis.summary or "(요약 없음)",
            keywords=", ".join(analysis.keywords) or "(없음)",
            safe_objects=SAFE_OBJECTS[domain_key],
            heading=heading,
            domain_note=domain_note,
            person_rule="" if not reason else ANON_PERSON_RULE.format(reason=reason),
            action_rule=(ACTION_RULE.format(actions=_action_for(analysis))
                         if domain_key == "taekwondo" else ""),
            used_block=used_block,
        )}]}],
        "generationConfig": {
            "temperature": 0.6,
            "responseMimeType": "application/json",
            "responseSchema": PLAN_SCHEMA,
        },
    }

    data = model_chain.call(
        lambda m: _call_gemini(payload, m), model, ILLUST_FALLBACK_MODELS
    )
    if not data:
        log.warning(f"  Plan 생성 실패 — 폴백 사용 ({heading[:24]})")
        return _fallback(analysis, heading, variant, eff_domain)

    scene = (data.get("scene") or "").strip()
    if not scene:
        return _fallback(analysis, heading, variant, eff_domain)

    count = int(data.get("subject_count", 1))
    shot = data.get("shot", "medium")
    composition = data.get("composition", "eye_level_side")

    if count > 1:
        log.info(f"  subject_count {count} → 1 (다인 구도는 얼굴이 뭉개진다)")
        count = 1

    # 익명 인물 자리는 얼굴 클로즈업을 피한다. 얼굴만 크게 잡힌 사진은
    # 익명화 문구가 붙어도 특정인의 초상처럼 읽힌다.
    if reason and count > 0 and shot == "close":
        log.info("  익명 인물 자리 — shot close → medium")
        shot = "medium"
        if composition == "close_up":
            composition = "eye_level_front"

    # scene 의 묘사와 shot 을 맞춘다.
    if CLOSE_HINT.search(scene) and shot == "wide":
        log.info("  scene 이 close-up 인데 shot 은 wide — close 로 맞춥니다")
        shot = "close"
        if composition == "wide_establishing":
            composition = "close_up"

    if count > 0 and shot == "wide":
        log.info("  shot wide → medium (인물이 있으면 넓게 잡지 않는다)")
        shot = "medium"
    if count > 0 and composition == "wide_establishing":
        composition = "eye_level_front"

    # 종목 표지가 없으면 생성 모델이 임의의 종목으로 채운다.
    # 조립부가 앵커 문구를 덧붙이지만, 문장 자체가 다른 종목을 가리키면
    # 앵커만으로는 되돌려지지 않는다.
    if not _has_domain_marker(scene, eff_domain):
        log.warning(
            f"  scene 에 '{eff_domain}' 종목 표지가 없습니다 — 폴백 대체: {scene[:56]}"
        )
        return _fallback(analysis, heading, variant, eff_domain)

    # LLM 이 people / faces / hands 를 금지어에 넣는 경우가 있다. 인물이
    # 있는 자리에서 이것들이 네거티브로 가면 얼굴과 손이 뭉개진다.
    raw = data.get("forbidden") or []
    if count > 0:
        blocked = {"people", "person", "human", "faces", "face",
                   "hands", "hand", "human figure", "athlete", "athletes"}
        raw = [t for t in raw if t.strip().lower() not in blocked]
    forbidden = list(dict.fromkeys(BASE_FORBIDDEN + raw))

    return VisualPlan(
        scene=scene,
        subject_count=count,
        composition=composition,
        shot=shot,
        mood=(data.get("mood") or "").strip(),
        aspect_ratio=data.get("aspect_ratio", "16:9"),
        forbidden=forbidden,
        heading=heading,
        heading_focus=(data.get("heading_focus") or "").strip(),
        category=analysis.category,
        domain=eff_domain,
        anonymous=bool(reason),
    )


def _too_similar(scene: str, used: list[str], threshold: float = 0.72) -> str | None:
    """이미 쓴 장면과 지나치게 비슷한가."""
    for u in used:
        if difflib.SequenceMatcher(None, scene.lower(), u.lower()).ratio() >= threshold:
            return u
    return None


def plan_all(
    analysis: ArticleAnalysis, headings: list[str], model: str
) -> list[VisualPlan]:
    """소제목마다 Plan 하나. 실패한 것은 폴백으로 채워 개수를 맞춘다.

    개수가 맞아야 호출부가 인덱스로 소제목과 짝지을 수 있다.
    """
    plans: list[VisualPlan] = []
    used: list[str] = []
    total = len(headings)

    for i, h in enumerate(headings, 1):
        try:
            p = plan_for(analysis, h, model, used_scenes=used,
                         variant=i - 1, index=i, total=total)
        except ModelExhausted as e:
            log.warning(f"모델 한도 소진 — 남은 Plan 은 폴백으로 채웁니다: {e}")
            dom, _ = _effective_domain(analysis, h, i, total)
            for j, x in enumerate(headings[i - 1:]):
                plans.append(_fallback(analysis, x, variant=i - 1 + j, domain=dom))
            break

        if _too_similar(p.scene, used):
            log.info("  이미 쓴 장면과 겹칩니다 — 폴백 변형으로 대체")
            p = _fallback(analysis, h, variant=i - 1, domain=p.domain)

        plans.append(p)
        used.append(p.scene)

        # 삽화가 어긋났을 때 로그만으로 원인을 짚을 수 있어야 한다.
        # scene 을 자르지 않는다 — 잘린 부분에 원인이 있는 경우가 많다.
        log.info(f"  ┌ Plan {i}/{total} — {h}")
        log.info(f"  │ scene  : {p.scene}")
        log.info(f"  │ focus  : {p.heading_focus or '(미기재)'}")
        log.info(f"  │ 사양   : {p.subject_count}인"
                 f"{' (익명)' if p.anonymous else ''} · {p.shot} · {p.composition} "
                 f"· {p.aspect_ratio} · 종목 {p.domain}")
        log.info(f"  └ mood   : {p.mood}")

    return plans

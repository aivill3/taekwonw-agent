"""1단계 — 기사 분석 (Analysis Stage).

기사에서 삽화 생성에 필요한 메타데이터를 뽑는다. 그림을 그리지 않고,
'무엇을 그려야 하는가'를 판단할 재료만 만든다.

고유명사 격리가 이 단계의 핵심이다:
  실명·소속·상표명·대회명·연도를 `identifiers` 로 따로 뺀다. 이 목록은
  프롬프트에 절대 전달되지 않으며, 5단계 하드 필터와 7단계 검수의
  금칙어 사전으로만 쓰인다.

  LLM 에게 "대회명을 쓰지 마세요" 라고 지시하는 방식은 확률적으로 샌다.
  아예 넘기지 않고, 나온 결과를 규칙으로 대조해 걸러내는 편이 확실하다.

격리만으로는 부족하다 — domain / occasion / setting_en 을 함께 뽑는 이유:
  고유명사를 전부 들어내면 2단계에 남는 정보가 category 라벨 하나뿐이다.
  e스포츠 대표팀 출정식 기사에서 33개를 격리했더니 'event' 만 남았고,
  2단계는 그것만 보고 시상대·메달·태권도 띠를 계획했다. 종목도 행사
  성격도 사라졌기 때문이다.

  격리(제거)와 일반화(치환)는 다른 일이다. '이상혁'은 지워야 하지만
  'a professional esports player' 는 남겨야 한다. 아래 세 필드가 그
  일반화된 잔여물이며, 프롬프트로 전달되는 유일한 맥락이다.

라벨 흔들림 방지:
  category / domain / occasion 을 enum 으로 강제한다. 자유 문자열로 두면
  '겨루기'/'경기'/'스파링' 같은 표기가 섞여 3단계 라이브러리 조회가
  매번 0건이 된다.
  confidence 가 낮으면 '기타'로 폴백한다 — 틀린 라벨로 엉뚱한 자산을
  고르는 것보다 폴백 자산이 낫다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict

import requests

from config.settings import GEMINI_API_KEY, REQUEST_TIMEOUT
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

log = get_logger(__name__)

# 라이브러리 폴더명과 1:1 대응한다. 값을 바꾸면 assets/ 구조도 함께 바꿔야 한다.
CATEGORIES = ("sparring", "poomsae", "demonstration", "event", "interview", "other")

# 한국어 표기 → enum. 프롬프트에 함께 보여줘 모델이 매핑을 이해하게 한다.
CATEGORY_LABELS = {
    "sparring": "겨루기",
    "poomsae": "품새",
    "demonstration": "시범·공연",
    "event": "행사·시상·협약",
    "interview": "인터뷰·인물",
    "other": "기타",
}

# 종목 영역. category 가 태권도 종목 체계라 종목이 다르면 전부 'event'/'other'
# 로 뭉개진다. 그 손실을 여기서 받는다.
#
# assets/ 폴더 구조와는 무관한 필드다. category 를 건드리지 않고 추가할 수
# 있어 3단계 라이브러리 조회는 그대로 동작한다.
DOMAINS = ("taekwondo", "other_martial_art", "other_sport", "esports", "non_sport")

DOMAIN_LABELS = {
    "taekwondo": "태권도",
    "other_martial_art": "태권도 외 무도 (유도·검도·복싱 등)",
    "other_sport": "무도가 아닌 스포츠 (축구·양궁·육상 등)",
    "esports": "e스포츠·게임",
    "non_sport": "스포츠가 아닌 사안 (행정·교육·산업 등)",
}

# 행사 성격. category='event' 하나가 출정식·시상식·협약식을 전부 삼킨다.
# 셋은 시각적으로 완전히 다른 장면이라(대회 전 / 대회 후 / 실내 서명)
# 구분하지 않으면 출정식 자리에 시상대가 들어간다.
OCCASIONS = (
    "send_off",     # 출정식·발대식·결단식 — 대회 '전'
    "award",        # 시상식·수여식 — 대회 '후'
    "agreement",    # 협약·위촉·임명
    "opening",      # 개막·창단·개소
    "press",        # 기자회견·인터뷰 자리
    "competition",  # 경기 진행 중
    "training",     # 훈련·수련
    "none",         # 위에 해당 없음
)

OCCASION_LABELS = {
    "send_off": "출정식·발대식 (대회 전)",
    "award": "시상식·수여식 (대회 후)",
    "agreement": "협약·위촉·임명",
    "opening": "개막·창단·개소",
    "press": "기자회견",
    "competition": "경기 진행",
    "training": "훈련·수련",
    "none": "해당 없음",
}

CONFIDENCE_FLOOR = 0.6  # 이 아래면 other 로 폴백

BODY_LIMIT = 3000  # 프롬프트에 넣을 본문 상한. 리드와 앞부분에 핵심이 있다.

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "domain": {"type": "string", "enum": list(DOMAINS)},
        "occasion": {"type": "string", "enum": list(OCCASIONS)},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "setting_en": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "identifiers": {
            "type": "object",
            "properties": {
                "people": {"type": "array", "items": {"type": "string"}},
                "organizations": {"type": "array", "items": {"type": "string"}},
                "events": {"type": "array", "items": {"type": "string"}},
                "brands": {"type": "array", "items": {"type": "string"}},
                "places": {"type": "array", "items": {"type": "string"}},
                "years": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["people", "organizations", "events", "brands", "places", "years"],
        },
    },
    "required": ["category", "domain", "occasion", "confidence", "summary",
                 "setting_en", "keywords", "identifiers"],
}

PROMPT = """다음 기사를 읽고 삽화 제작에 필요한 정보를 뽑아 주세요.

태권도 매체의 기사이지만, 태권도가 아닌 종목이나 사안을 다루는 기사도
들어옵니다. **기사에 실제로 나온 종목을 그대로 판단하세요.** 태권도가
아닌데 태권도로 분류하면 엉뚱한 그림이 만들어집니다.

## domain
기사가 다루는 종목·영역입니다.
{domains}

## category
장면의 성격입니다. 태권도 종목 체계를 기준으로 하며, 태권도가 아닌
기사는 대부분 event / interview / other 중 하나가 됩니다.
{labels}

## occasion
행사의 성격입니다. 시각적으로 완전히 다르므로 정확히 골라 주세요.
특히 **출정식(대회 전)과 시상식(대회 후)을 혼동하지 마세요.**
{occasions}

## confidence
category 분류 확신도를 0.0~1.0 으로 씁니다. 애매하면 낮게 주세요.

## summary
기사의 핵심 장면을 한국어 한 문장으로 씁니다. 그림으로 그릴 수 있는
'무슨 일이 일어나는 장면인지'를 씁니다.

## setting_en
**이 항목이 가장 중요합니다.**

기사의 장소·상황·복장을 **영어 한 문장**으로 씁니다.
고유명사는 절대 쓰지 말되, **종목과 행사 성격은 반드시 남깁니다.**
그림을 그리는 쪽은 이 문장 외에 기사 내용을 볼 수 없습니다.

| 기사 | 이렇게 쓰지 마세요 | 이렇게 쓰세요 |
|---|---|---|
| 이상혁 선수 아시안게임 e스포츠 출정식 | a send-off ceremony | a send-off ceremony for a national esports team, players in matching team jerseys, indoor auditorium with rows of seats |
| 무주 태권도원 제17회 총장배 겨루기 결승 | a taekwondo match | a taekwondo sparring match on a matted competition area, athletes in white dobok and unbranded electronic body protectors, indoor arena |
| 국기원-OO시 태권도 진흥 협약 체결 | a signing event | an indoor signing ceremony at a long table with document folders and flags, business attire |

종목을 빼면 안 됩니다. "a ceremony" 처럼 종목이 없는 문장은 실패입니다.

## keywords
그림 소재가 될 명사 3~6개. 한국어로 씁니다.
**대회명·인명·지명·소속은 넣지 마세요. 단, 종목명과 행사 종류는 넣습니다.**
- 좋은 예 (태권도 기사): 겨루기, 시상대, 도복, 관중석
- 좋은 예 (e스포츠 기사): e스포츠, 출정식, 유니폼, 경기용 부스
- 나쁜 예: 무주, 국기원, 김운용컵, 제17회

## identifiers
기사에 나오는 고유명사를 종류별로 **빠짐없이** 모읍니다.
이 목록은 그림에 절대 나오면 안 되는 것들을 걸러내는 데 쓰입니다.
- people: 사람 이름
- organizations: 기관·팀·학교·협회명
- events: 대회·행사명
- brands: 상표·제품명
- places: 지명·경기장명
- years: 연도 표기 (예: 2026, 제17회)

**주의:** 'e스포츠', '태권도', '축구' 같은 **종목명은 고유명사가 아닙니다.**
identifiers 에 넣지 마세요. 종목이 지워지면 그림을 만들 수 없습니다.

## 기사

제목: {title}

본문:
{body}

JSON 으로만 응답하세요."""


@dataclass
class Identifiers:
    """그림에 나오면 안 되는 고유명사. 프롬프트로 전달되지 않는다."""

    people: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    years: list[str] = field(default_factory=list)

    def all_terms(self) -> list[str]:
        """하드 필터·검수용 평면 목록. 짧은 토막은 뺀다.

        한 글자짜리가 섞이면 정상 프롬프트까지 걸러낸다.
        """
        terms: list[str] = []
        for group in asdict(self).values():
            terms.extend(t.strip() for t in group if len(t.strip()) >= 2)
        return sorted(set(terms), key=len, reverse=True)

    def to_dict(self) -> dict:
        return asdict(self)


# 종목명이 identifiers 로 잘못 분류되면 2단계에서 종목이 사라진다.
# 프롬프트로 막아도 확률적으로 새므로 코드에서 한 번 더 걷어낸다.
NOT_IDENTIFIERS = {
    "태권도", "e스포츠", "이스포츠", "esports", "게임", "유도", "검도", "복싱",
    "가라테", "합기도", "씨름", "축구", "야구", "농구", "배구", "양궁", "육상",
    "수영", "펜싱", "레슬링", "골프", "테니스", "품새", "겨루기", "시범",
}


@dataclass
class ArticleAnalysis:
    category: str = "other"
    domain: str = "taekwondo"
    occasion: str = "none"
    confidence: float = 0.0
    summary: str = ""
    setting_en: str = ""
    keywords: list[str] = field(default_factory=list)
    identifiers: Identifiers = field(default_factory=Identifiers)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identifiers"] = self.identifiers.to_dict()
        return d


def _call_gemini(payload: dict, model: str) -> dict | None:
    """구조화 출력 1회 호출. 실패 시 None (삽화는 폴백으로 진행)."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
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
                log.warning(f"기사 분석 응답 오류 [{resp.status_code}]: {resp.text[:150]}")
                return None
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"```(?:json)?", "", text).strip())
        except ModelExhausted:
            raise
        except Exception as e:
            log.warning(f"기사 분석 실패: {e}")
            return None
    return None


def _clean_identifier_group(items: list) -> list[str]:
    """종목명이 섞여 들어온 것을 걷어낸다."""
    out = []
    for t in items or []:
        s = str(t).strip()
        if not s:
            continue
        if s.lower() in NOT_IDENTIFIERS or s in NOT_IDENTIFIERS:
            continue
        out.append(s)
    return out


def analyze(title: str, body: str, model: str) -> ArticleAnalysis:
    """기사 → ArticleAnalysis. 실패하면 category='other' 인 빈 결과를 돌려준다.

    예외를 올리지 않는다. 삽화가 없어도 글은 성립하므로, 분석 실패로
    발행 전체를 멈추지 않는다.
    """
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY 가 없어 기사 분석을 건너뜁니다")
        return ArticleAnalysis()

    labels = "\n".join(f"- {k}: {v}" for k, v in CATEGORY_LABELS.items())
    domains = "\n".join(f"- {k}: {v}" for k, v in DOMAIN_LABELS.items())
    occasions = "\n".join(f"- {k}: {v}" for k, v in OCCASION_LABELS.items())

    payload = {
        "contents": [{"parts": [{"text": PROMPT.format(
            labels=labels,
            domains=domains,
            occasions=occasions,
            title=title,
            body=(body or "")[:BODY_LIMIT],
        )}]}],
        "generationConfig": {
            "temperature": 0.2,  # 분류 작업이라 낮게. 매번 같은 라벨이 나와야 한다.
            "responseMimeType": "application/json",
            "responseSchema": ANALYSIS_SCHEMA,
        },
    }

    data = _call_gemini(payload, model)
    if not data:
        return ArticleAnalysis()

    ids = data.get("identifiers") or {}
    result = ArticleAnalysis(
        category=data.get("category", "other"),
        domain=data.get("domain", "taekwondo"),
        occasion=data.get("occasion", "none"),
        confidence=float(data.get("confidence", 0.0)),
        summary=(data.get("summary") or "").strip(),
        setting_en=(data.get("setting_en") or "").strip(),
        keywords=[k.strip() for k in (data.get("keywords") or []) if k.strip()],
        identifiers=Identifiers(
            people=_clean_identifier_group(ids.get("people")),
            organizations=_clean_identifier_group(ids.get("organizations")),
            events=_clean_identifier_group(ids.get("events")),
            brands=_clean_identifier_group(ids.get("brands")),
            places=_clean_identifier_group(ids.get("places")),
            years=[str(y) for y in (ids.get("years") or [])],
        ),
    )

    if result.category not in CATEGORIES:
        log.warning(f"알 수 없는 카테고리 '{result.category}' → other")
        result.category = "other"
    if result.domain not in DOMAINS:
        log.warning(f"알 수 없는 도메인 '{result.domain}' → taekwondo")
        result.domain = "taekwondo"
    if result.occasion not in OCCASIONS:
        result.occasion = "none"

    # confidence 는 category 에만 적용한다. domain 은 폴백하지 않는다 —
    # 종목을 'other' 로 뭉개면 애초에 이 필드를 만든 이유가 없어진다.
    if result.confidence < CONFIDENCE_FLOOR and result.category != "other":
        log.info(
            f"분류 확신도 미달({result.confidence:.2f} < {CONFIDENCE_FLOOR}) "
            f"'{result.category}' → other"
        )
        result.category = "other"

    # setting_en 이 비면 2단계가 맥락 없이 계획하게 된다. 경고를 남겨
    # 나중에 로그만 보고도 원인을 짚을 수 있게 한다.
    if not result.setting_en:
        log.warning("setting_en 이 비었습니다 — 2단계가 카테고리만 보고 계획합니다")

    # 태권도 매체인데 태권도가 아닌 기사가 들어온 것은 그 자체로 신호다.
    if result.domain != "taekwondo":
        log.info(f"태권도 외 기사입니다: {DOMAIN_LABELS[result.domain]}")

    log.info(
        f"기사 분석: {CATEGORY_LABELS.get(result.category, result.category)} "
        f"({result.confidence:.2f}) · {DOMAIN_LABELS.get(result.domain)} · "
        f"{OCCASION_LABELS.get(result.occasion)} · "
        f"키워드 {len(result.keywords)}개 · "
        f"격리 고유명사 {len(result.identifiers.all_terms())}개"
    )
    log.info(f"  배경: {result.setting_en[:90]}")
    return result

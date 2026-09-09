"""블로그 삽화 생성 (Gemini 이미지 모델).

⚠️ 생성 범위 원칙 — 개념적 일러스트로 한정한다:
  뉴스 기반 블로그에서 실제처럼 보이는 AI 이미지를 쓰면 독자가 사건 사진으로
  오인할 수 있다. 그래서 다음을 금지한다.
    - 실제 사건·경기 장면의 재현
    - 실존 인물(선수·임원·기자)의 묘사
    - 특정 장소·건물·대회 현장의 사실적 재현
    - 사진처럼 보이는 렌더링, 얼굴이 식별되는 인물
  대신 태권도의 동작·상징·개념을 표현한 평면 일러스트만 만든다.

프롬프트는 소주제 텍스트를 그대로 넣지 않는다. 소주제에는 인물명·대회명이
들어 있어 그대로 전달하면 실제 장면 묘사로 흐르기 때문이다.
대신 소주제에서 '시각 개념'만 추출해(LLM #3) 일러스트 지시로 변환한다.
"""
import base64
import json
import time

import requests

from config.settings import GEMINI_API_KEY, IMAGE_DIR, REQUEST_TIMEOUT
from core.logger import get_logger
from core.prompt_loader import load_prompt
from tools.gemini_client import (
    API_BASE,
    MAX_RETRIES,
    build_chain,
    RETRYABLE_STATUS,
    SERVER_BACKOFF_BASE,
    ModelExhausted,
    api_url,
    is_daily_quota,
    looks_like_zero_quota,
    violated_quota_ids,
    wait_seconds,
)

log = get_logger(__name__)

# 이미지 모델은 텍스트 모델과 별도 할당량을 쓴다.
# 나아가 Imagen 과 Gemini 이미지 모델도 서로 별도 할당량을 쓴다.
# (실측: Gemini 이미지 3종이 모두 소진된 날에도 Imagen 은 0/25 로 남아 있었다)
# 그래서 Imagen 을 앞에 둔다. 뒤로 갈수록 소진되기 쉬운 순서다.
#
# 호출 형식이 둘로 갈린다. is_imagen() 으로 분기한다.
#   imagen-*   :predict        {"instances":[{"prompt":...}]}
#                              -> predictions[].bytesBase64Encoded
#   gemini-*   :generateContent {"contents":[{"parts":[{"text":...}]}]}
#                              -> candidates[0].content.parts[].inlineData.data
IMAGE_MODELS = [
    "imagen-4.0-fast-generate-001",
    "imagen-4.0-generate-001",
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
]


def is_imagen(model: str) -> bool:
    """Imagen 계열인지. 엔드포인트와 요청·응답 형식이 Gemini 와 다르다."""
    return model.startswith("imagen-")


def _image_url(model: str) -> str:
    """모델에 맞는 엔드포인트. api_url() 은 generateContent 전용이라 분기한다."""
    if is_imagen(model):
        return f"{API_BASE}/{model}:predict"
    return api_url(model)


def _image_payload(prompt: str, model: str) -> dict:
    """모델에 맞는 요청 본문."""
    text = prompt + STYLE_SUFFIX
    if is_imagen(model):
        return {
            "instances": [{"prompt": text}],
            # 1장만 받는다. 여러 장 받아도 쓰지 않으면서 할당량만 소모한다.
            "parameters": {"sampleCount": 1, "aspectRatio": "4:3"},
        }
    return {"contents": [{"parts": [{"text": text}]}]}


def _extract_image(body: dict, model: str) -> bytes | None:
    """응답에서 PNG 바이트를 꺼낸다. 형식이 다르므로 분기한다."""
    if is_imagen(model):
        for pred in body.get("predictions") or []:
            b64 = pred.get("bytesBase64Encoded")
            if b64:
                return base64.b64decode(b64)
        return None
    for part in body["candidates"][0]["content"]["parts"]:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    return None
REQUEST_INTERVAL = 6.0

# 시각 개념 추출(텍스트 모델)의 폴백 체인.
# 초안 작성과 같은 모델을 연달아 쓰면 분당 한도에 걸리므로, 다른 계열을 섞는다.
CONCEPT_FALLBACK_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
]

# 모든 이미지에 공통으로 붙는 스타일·금지 지시
STYLE_SUFFIX = (
    " Flat vector illustration, minimal geometric shapes, clean lines, "
    "limited color palette of deep blue, white and red accents. "
    "Stylized and abstract, clearly non-photographic. "
    "No text, no letters, no numbers, no logos, no national flags. "
    "No identifiable faces, no realistic human likeness, no photorealism, "
    "no depiction of a real event or real venue."
)

# 소주제 → 시각 개념 추출 프롬프트 (LLM #3)
CONCEPT_PROMPT = load_prompt("image/illustration_concept.md")

CONCEPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "prompts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "prompt": {"type": "STRING"},
                },
                "required": ["index", "prompt"],
            },
        }
    },
    "required": ["prompts"],
}


def _request_concepts(headings: list[str], model: str) -> dict[int, str]:
    """단일 모델로 시각 개념 1회 요청. 실패 시 빈 dict.
    모델을 더 쓸 수 없으면 ModelExhausted 를 던져 폴백을 유도한다."""
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(headings, 1))
    payload = {
        "contents": [{"parts": [{"text": CONCEPT_PROMPT.format(headings=numbered)}]}],
        "generationConfig": {
            "temperature": 0.9,
            "responseMimeType": "application/json",
            "responseSchema": CONCEPT_SCHEMA,
        },
    }
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=REQUEST_TIMEOUT * 6,
            )

            if resp.status_code == 429:
                try:
                    body_json = resp.json()
                except ValueError:
                    body_json = {}
                violated = violated_quota_ids(body_json)
                if looks_like_zero_quota(body_json) or is_daily_quota(body_json):
                    raise ModelExhausted(f"한도: {', '.join(violated) or '(파싱 불가)'}")
                if attempt < MAX_RETRIES:
                    wait = wait_seconds(resp.text, attempt)
                    log.warning(f"시각 개념 RPM 초과, {wait:.0f}초 후 재시도")
                    time.sleep(wait)
                    continue
                raise ModelExhausted("RPM 재시도 초과")

            if resp.status_code == 404:
                raise ModelExhausted("모델 사용 불가(404)")

            if resp.status_code in RETRYABLE_STATUS:
                if attempt < MAX_RETRIES:
                    time.sleep(SERVER_BACKOFF_BASE * (2 ** attempt))
                    continue
                return {}

            if not resp.ok:
                log.warning(f"시각 개념 추출 실패 [{resp.status_code}]: {resp.text[:150]}")
                return {}

            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            items = json.loads(text).get("prompts", [])
            return {
                int(it["index"]) - 1: str(it["prompt"]).strip()
                for it in items
                if str(it.get("prompt", "")).strip()
            }
        except ModelExhausted:
            raise
        except Exception as e:
            log.warning(f"시각 개념 추출 오류: {e}")
            return {}
    return {}


def build_concepts(headings: list[str], text_model: str) -> dict[int, str]:
    """소제목 → 일러스트 프롬프트. 실패 시 빈 dict (이미지 없이 진행).

    초안 작성기와 같은 폴백 체인을 쓴다. 이 단계가 막히면 이미지 모델은
    호출조차 되지 않아 Imagen 할당량이 남아 있어도 삽화가 0장이 된다.
    (실측: 초안 작성 1초 뒤 호출돼 RPM 429 → 재시도 없이 즉시 포기)

    첫 호출 전에 간격을 두는 이유도 같다. 바로 앞에서 초안 작성이
    텍스트 모델을 쓰고 끝났으므로, 붙여 호출하면 분당 한도에 걸린다.
    """
    if not headings:
        return {}

    time.sleep(REQUEST_INTERVAL)  # 직전 초안 작성 호출과 간격 확보

    for model in build_chain(text_model, CONCEPT_FALLBACK_MODELS):
        try:
            concepts = _request_concepts(headings, model)
        except ModelExhausted as e:
            log.warning(f"시각 개념 모델 '{model}' 전환. {e}")
            continue
        if concepts:
            return concepts
        log.warning(f"시각 개념 모델 '{model}' 응답이 비어 다음 모델로 전환")

    log.warning("모든 텍스트 모델의 한도가 소진되어 시각 개념을 얻지 못했습니다")
    return {}


def _generate_image(prompt: str, model: str) -> bytes | None:
    """이미지 1장 생성 → PNG 바이트. 실패 시 None.
    모델을 더 쓸 수 없으면 ModelExhausted 를 던진다."""
    payload = _image_payload(prompt, model)
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                _image_url(model),
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=REQUEST_TIMEOUT * 12,
            )

            if resp.status_code == 429:
                try:
                    body_json = resp.json()
                except ValueError:
                    body_json = {}
                violated = violated_quota_ids(body_json)
                if looks_like_zero_quota(body_json) or is_daily_quota(body_json):
                    raise ModelExhausted(f"이미지 모델 한도: {', '.join(violated)}")
                if attempt < MAX_RETRIES:
                    wait = wait_seconds(resp.text, attempt)
                    log.warning(f"이미지 RPM 초과, {wait:.0f}초 후 재시도")
                    time.sleep(wait)
                    continue
                return None

            if resp.status_code == 404:
                raise ModelExhausted(f"이미지 모델 사용 불가(404)")

            if resp.status_code in RETRYABLE_STATUS:
                if attempt < MAX_RETRIES:
                    time.sleep(SERVER_BACKOFF_BASE * (2 ** attempt))
                    continue
                return None

            if not resp.ok:
                log.warning(f"이미지 생성 오류 [{resp.status_code}]: {resp.text[:150]}")
                return None

            # 응답에서 base64 이미지를 꺼낸다 (형식은 모델 계열에 따라 다름)
            data = _extract_image(resp.json(), model)
            if data:
                return data
            log.warning("응답에 이미지 데이터가 없습니다")
            return None

        except ModelExhausted:
            raise
        except Exception as e:
            log.warning(f"이미지 생성 실패: {e}")
            return None
    return None


def generate_for_sections(
    headings: list[str], slug: str, text_model: str
) -> dict[int, dict]:
    """소제목별 삽화 1장씩 생성해 파일로 저장.
    {섹션 인덱스: {"path": Path, "caption": str}}  (stockimage와 형식 통일)

    이미지가 없어도 글은 성립하므로, 실패한 섹션은 조용히 건너뛴다.
    """
    if not GEMINI_API_KEY or not headings:
        return {}

    concepts = build_concepts(headings, text_model)
    if not concepts:
        log.warning("시각 개념을 얻지 못해 이미지 생성을 건너뜁니다")
        return {}

    result: dict[int, dict] = {}
    model_idx = 0
    first = True

    for idx in sorted(concepts):
        if model_idx >= len(IMAGE_MODELS):
            log.warning("모든 이미지 모델의 한도가 소진되어 중단")
            break
        data = None
        while model_idx < len(IMAGE_MODELS):
            if not first:
                time.sleep(REQUEST_INTERVAL)
            first = False
            try:
                data = _generate_image(concepts[idx], IMAGE_MODELS[model_idx])
                break
            except ModelExhausted as e:
                log.warning(f"이미지 모델 '{IMAGE_MODELS[model_idx]}' 전환. {e}")
                model_idx += 1

        if not data:
            continue

        path = IMAGE_DIR / f"{slug}_{idx + 1}.png"
        path.write_bytes(data)
        result[idx] = {"path": path, "caption": "AI로 생성한 개념 일러스트입니다"}
        log.info(f"삽화 생성: {path.name} ({len(data) // 1024}KB)")

    log.info(f"삽화 {len(result)}/{len(headings)}장 생성")
    return result
"""소주제 생성 (LLM #1: Gemini Flash).

파이프라인에서 LLM을 호출하는 두 지점 중 첫 번째다.
  #1 소주제 생성 (여기)  — 사람이 승인 게이트에서 판단할 재료를 만든다
  #2 블로그 작성 (별도)  — 승인된 소주제로 본문을 쓴다
선정·랭킹·검증은 결정론적 코드가 담당하고, LLM은 생성 작업만 맡는다.

설계:
  - responseSchema(structured output)로 JSON 배열을 강제 → 파싱 실패 최소화
  - 기사 단위 실패 격리: 한 기사의 생성 실패가 전체를 멈추지 않는다
  - 본문은 PROMPT_BODY_LIMIT까지만 전달 (토큰 절약, 리드+본문 앞부분에 핵심이 있음)

무료 티어 429 대응 (두 종류를 구분해야 한다):
  - RPM/TPM 초과: 잠시 기다리면 풀린다
      → 응답의 retryDelay를 우선 따르고, 없으면 지수 백오프로 재시도
  - RPD(일일 한도) 초과: 태평양시 자정까지 회복되지 않는다
      → 재시도가 무의미하므로 즉시 전체 중단. 이미 생성된 기사는 저장한다(부분 성공 보존)
  - 평상시에도 REQUEST_INTERVAL 간격을 둬서 애초에 RPM에 닿지 않게 한다

일시 장애(5xx) 대응:
  - 503(UNAVAILABLE, 모델 과부하) / 500 / 504 는 서버 측 일시 장애다.
    할당량과 무관하므로 짧게 기다린 뒤 재시도한다.

배치 호출 (RPD 절약):
  RPD는 '요청 건수' 기준이므로 여러 기사를 한 요청에 묶으면 소모가 줄어든다.
  (배치 3 → RPD 1/3). TPM(250K)은 여유가 크므로 토큰은 제약이 아니다.
  - 배치가 실패하면 그 배치만 개별 호출로 재시도한다 (5건 통째 유실 방지)
  - SUBTOPIC_BATCH_SIZE=1 이면 개별 호출과 동일하게 동작한다
  - 여러 기사를 한 프롬프트에 넣으면 기사 간 내용이 섞일 위험이 있어
    프롬프트에 독립 처리를 명시하고, 응답을 article_index로 매칭한다

모델 폴백:
  RPD(일일 요청 수)는 모델별로 따로 집계된다. 따라서 한 모델의 일일 한도가
  소진되면 다음 모델로 갈아타 계속 진행할 수 있다.
  - 현재 모델이 RPD 소진/미지원(404)이면 폴백 체인의 다음 모델로 전환한다
  - 전환된 모델은 이후 기사에도 계속 쓰인다 (매번 앞 모델을 다시 시도하지 않음)
  - 체인의 모든 모델이 소진되면 그때 중단한다
"""
import json
import re
import time

import requests

from config.settings import GEMINI_API_KEY, REQUEST_TIMEOUT
from config.draft_config import SUBTOPIC_BATCH_SIZE, SUBTOPIC_FALLBACK_MODELS as GEMINI_FALLBACK_MODELS, SUBTOPIC_MODEL as GEMINI_MODEL
from core.logger import get_logger
from config.draft_config import SOLO_THRESHOLD
from core.prompt_loader import load_prompt
from tools.gemini_client import (
    MAX_RETRIES,
    RETRYABLE_STATUS,
    SERVER_BACKOFF_BASE,
    ModelExhausted,
    api_url,
    build_chain,
    is_daily_quota,
    looks_like_zero_quota,
    violated_quota_ids,
    wait_seconds,
)
from core.article_models import Article

log = get_logger(__name__)



def model_chain() -> list[str]:
    """소주제 생성용 모델 체인 (Lite 우선 — 호출이 잦고 정형 작업이라 충분)."""
    return build_chain(GEMINI_MODEL, GEMINI_FALLBACK_MODELS)

# 소주제 개수는 원문 정보량에 맞춘다.
# 짧은 단신에서 4개를 뽑으면 서로 겹치고, 글도 같은 사실을 되풀이하게 된다.
# (실측: 600자짜리 단신 → 4개 소주제 → 섹션 1·2가 동일 내용 반복)
# 소주제 = 초안의 챕터 하나. 그래서 개수를 발행 방식에 맞춰야 한다.
#
#   단독 발행(본문 SOLO_THRESHOLD 이상)  기사 하나가 글 한 편 -> 챕터 4개
#   묶음 발행(그 미만)                    기사 하나가 챕터 하나 -> 소주제 1개
#
# 묶음 대상에 3~4개를 만들면 DraftBrief.chapter_count 가 소주제를 합산하므로
# 3건만 묶어도 챕터가 9~12개가 된다. MAX_CHAPTERS(5)를 크게 넘고, 원래 얇은
# 기사라 챕터당 쓸 내용이 없어 창작이 시작된다. 묶음의 목적 자체가 무너진다.
SUBTOPIC_SOLO = 4      # 단독 발행 기사
SUBTOPIC_BUNDLE = 1    # 묶음 발행 기사 (기사 1건 = 챕터 1개)

# 하위 호환: 배치 응답 스키마의 minItems/maxItems 범위로 쓰인다.
SUBTOPIC_MIN = SUBTOPIC_BUNDLE
SUBTOPIC_MAX = SUBTOPIC_SOLO
PROMPT_BODY_LIMIT = 3000    # 본문 전달 상한 (자)
REQUEST_INTERVAL = 5.0      # 요청 간 최소 간격(초). 무료 티어 RPM 여유 확보용



def subtopic_count(body: str) -> int:
    """발행 방식에 따른 소주제 개수.

    기준은 config.draft_config.SOLO_THRESHOLD 하나를 공유한다.
    노션 '발행구분'(classify_publish_mode)과 같은 값을 봐야, 화면에 '묶음'으로
    표시된 기사가 소주제 4개를 받는 모순이 생기지 않는다.
    """
    return SUBTOPIC_SOLO if len(body) >= SOLO_THRESHOLD else SUBTOPIC_BUNDLE


# 출력 스키마: 소주제 문자열 배열만 받는다
def _response_schema(count: int) -> dict:
    return {
        "type": "OBJECT",
        "properties": {
            "subtopics": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "minItems": count,
                "maxItems": count,
            }
        },
        "required": ["subtopics"],
    }

# 배치 응답 스키마: 기사별 결과를 article_index로 매칭한다
BATCH_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "article_index": {"type": "INTEGER"},
                    "subtopics": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "minItems": SUBTOPIC_MIN,
                        "maxItems": SUBTOPIC_MAX,
                    },
                },
                "required": ["article_index", "subtopics"],
            },
        }
    },
    "required": ["results"],
}

BATCH_PROMPT = load_prompt("subtopic/batch.md")

PROMPT = load_prompt("subtopic/single.md")


def _build_payload(article: Article) -> dict:
    count = subtopic_count(article.body_clean)
    prompt = PROMPT.format(
        count=count,
        title=article.title,
        body=article.body_clean[:PROMPT_BODY_LIMIT],
    )
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
            "responseSchema": _response_schema(count),
        },
    }


def _parse_response(text: str, article: Article) -> list[str]:
    """응답 JSON에서 소주제 목록 추출 + 방어적 검증."""
    subtopics = json.loads(text).get("subtopics", [])
    cleaned = [str(s).strip() for s in subtopics if str(s).strip()]
    need = subtopic_count(article.body_clean)
    if len(cleaned) < need:
        log.warning(f"소주제 부족({len(cleaned)}/{need}개), 제외: {article.title[:40]}")
        return []
    return cleaned[:need]


def _build_batch_payload(articles: list[Article]) -> dict:
    blocks = []
    for i, a in enumerate(articles, 1):
        need = subtopic_count(a.body_clean)
        blocks.append(
            f"[기사 {i}] (소주제 {need}개 필요)\n제목: {a.title}\n"
            f"본문:\n{a.body_clean[:PROMPT_BODY_LIMIT]}"
        )
    prompt = BATCH_PROMPT.format(n=len(articles), articles="\n\n".join(blocks))
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
            "responseSchema": BATCH_RESPONSE_SCHEMA,
        },
    }


def _parse_batch_response(text: str, batch: list[Article]) -> dict[int, list[str]]:
    """배치 응답 → {0-based 기사 인덱스: 소주제 목록}. 유효 항목만 담는다."""
    results = json.loads(text).get("results", [])
    parsed: dict[int, list[str]] = {}
    for item in results:
        try:
            idx = int(item["article_index"]) - 1  # 프롬프트는 1-based
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= idx < len(batch)):
            log.warning(f"배치 응답의 article_index 범위 이탈: {item.get('article_index')}")
            continue
        subs = [str(s).strip() for s in item.get("subtopics", []) if str(s).strip()]
        need = subtopic_count(batch[idx].body_clean)
        if len(subs) >= need:
            parsed[idx] = subs[:need]
        else:
            log.warning(
                f"소주제 부족({len(subs)}/{need}개): {batch[idx].title[:35]}"
            )
    return parsed


def generate_batch(batch: list[Article], model: str) -> dict[int, list[str]]:
    """기사 여러 건을 1회 요청으로 처리. 실패 시 빈 dict.
    모델을 더 쓸 수 없으면 ModelExhausted 를 던진다 (호출자가 모델 전환)."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=_build_batch_payload(batch),
                timeout=REQUEST_TIMEOUT * 12,  # 배치는 개별보다 오래 걸린다
            )

            if resp.status_code == 429:
                body = resp.text
                try:
                    body_json = resp.json()
                except ValueError:
                    body_json = {}
                violated = violated_quota_ids(body_json)
                log.warning(f"429 위반 할당량: {violated or '(파싱 불가)'}")
                if looks_like_zero_quota(body_json):
                    raise ModelExhausted(f"무료 할당량 없음: {', '.join(violated)}")
                if is_daily_quota(body_json):
                    raise ModelExhausted(f"일일 한도 소진: {', '.join(violated)}")
                if attempt < MAX_RETRIES:
                    wait = wait_seconds(body, attempt)
                    log.warning(f"RPM 초과(429), {wait:.0f}초 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                return {}

            if resp.status_code == 404:
                raise ModelExhausted(f"모델 사용 불가(404): {resp.text[:150]}")

            if resp.status_code in RETRYABLE_STATUS:
                if attempt < MAX_RETRIES:
                    wait = SERVER_BACKOFF_BASE * (2 ** attempt)
                    log.warning(
                        f"서버 일시 장애({resp.status_code}), {wait:.0f}초 후 재시도 "
                        f"({attempt + 1}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                return {}

            if not resp.ok:
                log.warning(f"Gemini 응답 오류 [{resp.status_code}]: {resp.text[:200]}")
                return {}

            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _parse_batch_response(text, batch)

        except ModelExhausted:
            raise
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            log.warning(f"배치 응답 파싱 실패: {e}")
            return {}
        except Exception as e:
            log.warning(f"배치 생성 실패: {e}")
            return {}
    return {}


def generate_one(article: Article, model: str) -> list[str]:
    """기사 1건의 소주제 목록 생성. 실패 시 빈 리스트.
    모델을 더 쓸 수 없으면 ModelExhausted 를 던진다 (호출자가 모델 전환)."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=_build_payload(article),
                timeout=REQUEST_TIMEOUT * 6,  # 생성은 검색·저장보다 오래 걸린다
            )

            if resp.status_code == 429:
                body = resp.text
                try:
                    body_json = resp.json()
                except ValueError:
                    body_json = {}

                # 위반 항목을 진단용으로 남긴다 (RPM/RPD 판정 근거)
                violated = violated_quota_ids(body_json)
                log.warning(f"429 위반 할당량: {violated or '(파싱 불가)'}")
                log.debug(f"429 응답 전문: {body}")

                # 분당·일일·토큰이 동시 위반 = 이 모델의 무료 할당량이 0
                if looks_like_zero_quota(body_json):
                    raise ModelExhausted(f"무료 할당량 없음: {', '.join(violated)}")
                # 일일 한도(RPD) 소진: 기다려도 안 풀림 → 다른 모델로 전환
                if is_daily_quota(body_json):
                    raise ModelExhausted(f"일일 한도 소진: {', '.join(violated)}")
                if attempt < MAX_RETRIES:
                    wait = wait_seconds(body, attempt)
                    log.warning(
                        f"RPM 초과(429), {wait:.0f}초 후 재시도 "
                        f"({attempt + 1}/{MAX_RETRIES}): {article.title[:30]}"
                    )
                    time.sleep(wait)
                    continue
                log.warning(f"429 재시도 초과, 제외: {article.title[:40]}")
                return []

            # 404: 이 모델을 쓸 수 없음(정리된 구세대 등) → 다음 모델로
            if resp.status_code == 404:
                raise ModelExhausted(f"모델 사용 불가(404): {resp.text[:150]}")

            # 5xx: 서버 측 일시 장애 → 짧게 대기 후 재시도 (할당량과 무관)
            if resp.status_code in RETRYABLE_STATUS:
                if attempt < MAX_RETRIES:
                    wait = SERVER_BACKOFF_BASE * (2 ** attempt)
                    log.warning(
                        f"서버 일시 장애({resp.status_code}), {wait:.0f}초 후 재시도 "
                        f"({attempt + 1}/{MAX_RETRIES}): {article.title[:30]}"
                    )
                    time.sleep(wait)
                    continue
                log.warning(
                    f"{resp.status_code} 재시도 초과, 제외: {article.title[:40]}"
                )
                return []

            if not resp.ok:
                log.warning(f"Gemini 응답 오류 [{resp.status_code}]: {resp.text[:200]}")
                return []

            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return _parse_response(text, article)

        except ModelExhausted:
            raise
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            log.warning(f"Gemini 응답 파싱 실패 ({article.title[:40]}): {e}")
            return []
        except Exception as e:
            log.warning(f"소주제 생성 실패 ({article.title[:40]}): {e}")
            return []
    return []


def _attach(article: Article, subtopics: list[str], result: list[Article]) -> None:
    article.subtopics = subtopics
    result.append(article)
    log.info(f"소주제 생성: {article.title[:40]}")
    for i, s in enumerate(subtopics, 1):
        log.info(f"    {i}. {s}")


def generate_all(articles: list[Article]) -> list[Article]:
    """선정된 기사들에 소주제를 채운다. 생성 실패 건은 제외한다.

    처리 구조:
      기사들을 SUBTOPIC_BATCH_SIZE 단위로 묶어 1회 요청으로 처리한다(RPD 절약).
      배치가 실패하거나 일부 기사가 응답에서 누락되면, 그 기사만 개별 호출로 재시도한다.
      모델 한도가 소진되면 폴백 체인의 다음 모델로 전환한다.

    병렬 처리하지 않는 이유: 무료 티어 RPM이 빡빡해서 병렬로 던지면 즉시 429가 난다.
    """
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY 가 없습니다. .env 를 확인하세요.")
        return []

    chain = model_chain()
    batch_size = max(1, SUBTOPIC_BATCH_SIZE)
    batches = [
        articles[i : i + batch_size] for i in range(0, len(articles), batch_size)
    ]
    log.info(
        f"{len(articles)}건 소주제 생성 시작 (모델: {chain[0]}, 폴백 {len(chain) - 1}개, "
        f"배치 {batch_size} → 요청 {len(batches)}회 예상)"
    )

    result: list[Article] = []
    model_idx = 0
    first_request = True

    def current_model() -> str | None:
        return chain[model_idx] if model_idx < len(chain) else None

    def advance_model(reason: str) -> None:
        """모델 소진 → 다음 폴백으로 전환."""
        nonlocal model_idx
        log.warning(f"모델 '{chain[model_idx]}' 사용 불가 → 전환. {reason}")
        model_idx += 1
        if model_idx < len(chain):
            log.info(f"폴백 모델로 전환: {chain[model_idx]}")

    def call(fn, *args):
        """모델 전환을 처리하며 호출. 반환값 또는 None(전체 소진)."""
        nonlocal first_request
        while current_model():
            if not first_request:
                time.sleep(REQUEST_INTERVAL)  # RPM 여유 확보
            first_request = False
            try:
                return fn(*args, current_model())
            except ModelExhausted as e:
                advance_model(str(e))
        return None

    for bi, batch in enumerate(batches, 1):
        if not current_model():
            break

        log.info(f"배치 {bi}/{len(batches)} ({len(batch)}건) 요청")
        parsed = call(generate_batch, batch)
        if parsed is None:
            break  # 전체 모델 소진

        # 배치에서 못 받은 기사만 개별 호출로 폴백 (배치 통째 유실 방지)
        missing = [i for i in range(len(batch)) if i not in parsed]
        if missing:
            log.warning(
                f"배치 {bi}: {len(missing)}건 누락 → 개별 호출로 재시도 "
                f"(RPD {len(missing)}건 추가 소모)"
            )
        for i, article in enumerate(batch):
            if i in parsed:
                _attach(article, parsed[i], result)
                continue
            subs = call(generate_one, article)
            if subs is None:
                break  # 전체 모델 소진
            if subs:
                _attach(article, subs, result)
        else:
            continue
        break

    if not current_model():
        log.error(
            f"모든 모델({len(chain)}개)의 한도가 소진되어 중단 "
            f"({len(result)}/{len(articles)}건 처리). "
            f"태평양시 자정 이후 리셋되며, 이미 생성된 건은 저장됩니다."
        )

    used = current_model() or "없음"
    log.info(f"소주제 생성 완료 {len(result)}/{len(articles)}건 (사용 모델: {used})")
    return result
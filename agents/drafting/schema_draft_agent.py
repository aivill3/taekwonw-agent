"""블로그 글 작성 (LLM #2: Gemini).

파이프라인에서 LLM을 호출하는 두 지점 중 두 번째다.
  #1 소주제 생성 (subtopic.py) — 사람이 승인 게이트에서 판단할 재료
  #2 블로그 작성 (여기)        — 승인된 소주제로 글 한 편을 쓴다

구조:
  승인된 기사 1건 = 글 1편.
  소주제 4개가 각각 챕터 하나가 되고, 그 아래 본문이 붙는다.
  출력은 마크다운이 아니라 네이버 블로그 에디터에 그대로 붙여넣는 평문이다.

원문 기사를 함께 전달하는 이유:
  소주제만으로 쓰면 LLM이 기사에 없는 내용을 채워 넣는다(환각).
  원문을 근거로 제시하고 '원문에 있는 사실만 사용'을 명시해,
  이후 결정론적 팩트체크 단계에서 걸릴 내용을 미리 줄인다.

모델 선택:
  소주제 생성과 다른 체인을 쓴다. 이 작업은 긴 원문을 읽고 장문을 쓰기 때문에
  문맥 파악·표현력·환각 억제가 중요해 Flash를 우선한다.
  호출 수가 적어(승인 건수만큼) Flash의 빡빡한 RPD로도 감당된다.

프롬프트:
  draft_prompt.build_prompt() 가 조립한다. writing_guide.md 전문(규칙서)과
  BM25로 검색한 과거 글 3편이 함께 들어간다. 여기서 프롬프트를 만들지 않는다.
  build_prompt() 는 프롬프트와 함께 SeoConfig 도 돌려주는데, 프롬프트가 지시한
  분량·챕터 수를 검사기가 그대로 쓰게 하기 위한 것이다.

JSON 스키마를 쓰지 않는 이유:
  스키마로 받으면 구조는 보장되지만 heading/paragraphs 배열이라
  줄바꿈 위치를 LLM이 정할 수 없다. 한 줄 15~20자로 끊는 것이 이 블로그의
  정체성이므로(가이드 2장) 평문으로 받고 postprocess() 로 정규화한다.

429/5xx 대응과 재시도 로직은 subtopic.py의 것을 재사용하되, 두 가지를 더한다.
  - 타임아웃·연결 끊김(응답 코드 없는 실패)도 재시도한다. 장문 생성은 요청이
    3분까지 열려 있어 그만큼 네트워크 오류를 만날 확률이 높다.
  - 재시도 대기를 더 길게(12초 기준) 잡고 지터를 준다. 백엔드 과부하가 원인일 때
    몇 초 뒤 재시도는 같은 상태를 만난다.
"""
import random
import time

import requests

from config.settings import GEMINI_API_KEY, REQUEST_TIMEOUT
from config.draft_config import (
    SCHEMA_BODY_LIMIT as BODY_LIMIT,
    SCHEMA_LENGTH_RATIO as LENGTH_RATIO,
    SCHEMA_MAX_LENGTH as MAX_LENGTH,
    SCHEMA_MIN_TARGET as MIN_TARGET,
    SCHEMA_REQUEST_INTERVAL as REQUEST_INTERVAL,
    SCHEMA_TARGET_LENGTH as TARGET_LENGTH,
    WRITER_FALLBACK_MODELS as GEMINI_WRITER_FALLBACK_MODELS,
    WRITER_MODEL as GEMINI_WRITER_MODEL,
)
from core.logger import get_logger
from core.text_metrics import prose_chars
from agents.drafting.corpus_retriever import BlogCorpus
from agents.drafting.draft_prompt import DraftBrief, SourceArticle, build_prompt
from agents.drafting.draft_postprocessor import postprocess
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

from tools.model_state import mark_exhausted, usable_chain

log = get_logger(__name__)


class TransientModelFailure(ModelExhausted):
    """5xx·통신 오류로 모델을 바꿔야 하지만, 일일 한도 소진은 아닌 경우.

    write_all 은 ModelExhausted 를 받으면 폴백으로 넘어가는데, 그 전환 이유가
    '한도 소진'인지 '일시 장애'인지 구분이 필요하다. 구글 쪽 일시 장애까지
    소진으로 기록하면 멀쩡한 모델이 하루 종일 차단된다.
    """

# 분량 기준은 config/draft_config.py 의 SCHEMA_* 가 단일 출처다.
# quality_gate.build_seo_config() 가 같은 상수를 읽어 검사하므로, 여기에
# 사본을 두면 "1,800자로 쓰라 시키고 다른 기준으로 검사"하는 모순이 난다.
# 모듈 안에서는 짧은 이름(BODY_LIMIT 등)으로 그대로 쓴다.

# 네트워크 계층 오류: 응답 코드가 없어 RETRYABLE_STATUS 로는 못 잡는다.
# 장문 생성은 요청이 3분까지 열려 있어 그만큼 타임아웃·연결 끊김을 만날 확률이
# 높은데, 일반 Exception 으로 흘려보내면 재시도가 남아 있어도 즉시 포기하게 된다.
# (실측 2026-08-14: 503 재시도 1회 뒤 타임아웃 → 남은 재시도 2회를 쓰지 못하고
#  기사 1건 유실. 3분을 기다린 끝에 결과 없이 버린 셈)
_NETWORK_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)

# 5xx·네트워크 오류의 재시도 대기(초). gemini_client 의 기본값보다 길게 잡는다.
# 그쪽은 짧은 소주제 요청 기준인데, 장문 생성은 백엔드가 과부하일 때 실패하므로
# 몇 초 뒤에 다시 던져봐야 같은 상태를 만난다.
DRAFT_BACKOFF_BASE = max(SERVER_BACKOFF_BASE, 12.0)


def _backoff(attempt: int) -> float:
    """지수 백오프 + 지터. 12 → 24 → 48초 근처.

    지터가 없으면 여러 요청이 같은 시각에 재시도해 과부하를 재현한다.
    """
    return DRAFT_BACKOFF_BASE * (2 ** attempt) * random.uniform(0.8, 1.2)

# 프롬프트는 draft_prompt.build_prompt() 가 조립한다 (가이드 전문 + RAG).


def target_length(body: str) -> int:
    """원문 길이에 맞춘 목표 분량.

    짧은 단신을 상한까지 늘리면 늘어난 만큼이 수식어와 중복으로 채워진다.
    원문의 LENGTH_RATIO 배를 넘지 않도록 제한한다.
    """
    return max(MIN_TARGET, min(TARGET_LENGTH, int(len(body) * LENGTH_RATIO)))


def to_source(item: dict) -> SourceArticle:
    """노션 항목(dict) → 프롬프트 조립용 SourceArticle."""
    return SourceArticle(
        title=item["title"],
        url=item.get("url", ""),
        date=item.get("date", ""),
        source=item.get("source", ""),
        subtopics=list(item.get("subtopics", [])),
        matched_keyword=item.get("matched_keyword", ""),
        content=item.get("body", "")[:BODY_LIMIT],
    )


def _build_payload(
    item: dict, corpus: BlogCorpus | None
) -> tuple[dict, list]:
    """Gemini 요청 본문을 만든다. (payload, 참고한 과거 글)

    item["brief"] 가 있으면 묶음이다. 없으면 기사 1건을 묶음으로 감싼다.
    프롬프트 조립은 build_prompt() 가 하고, 여기서는 생성 파라미터만 붙인다.
    """
    brief = item.get("brief") or DraftBrief.from_article(to_source(item))
    prompt, hits, seo = build_prompt(brief, corpus)
    # 프롬프트가 지시한 분량·챕터 수를 검사기가 그대로 쓰게 넘긴다.
    # 없으면 '1,300자로 쓰세요' 시켜놓고 '1,600자 미만' 경고가 뜬다.
    item["seo_config"] = seo
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # 소주제 생성(0.7)보다 조금 높게 — 문장 다양성.
            # responseSchema 를 쓰지 않는다. 평문을 그대로 받는다.
            "temperature": 0.8,
        },
    }
    return payload, hits


def enforce_length(markdown: str, limit: int = MAX_LENGTH) -> str:
    """Notion 속성 상한을 넘으면 문단 경계에서 잘라낸다.

    프롬프트로 분량을 지시하지만 LLM이 초과할 수 있어 마지막 안전장치를 둔다.
    문장 중간이 아니라 문단 단위로 끊어야 글이 덜 어색하다.
    """
    if len(markdown) <= limit:
        return markdown

    kept: list[str] = []
    length = 0
    for para in markdown.split("\n\n"):
        addition = len(para) + (2 if kept else 0)
        if length + addition > limit:
            break
        kept.append(para)
        length += addition

    result = "\n\n".join(kept).strip()
    if not result:  # 첫 문단부터 상한을 넘는 예외 상황
        result = markdown[:limit].rstrip()
    log.warning(f"분량 초과({len(markdown)}자) → {len(result)}자로 절단")
    return result


def _generate(item: dict, model: str, corpus: BlogCorpus | None) -> str:
    """글 1편 생성 → 발행 형식 평문. 실패 시 빈 문자열.
    모델을 더 쓸 수 없으면 ModelExhausted 를 던진다."""
    payload, hits = _build_payload(item, corpus)
    if hits:
        log.info(f"  참고한 과거 글 {len(hits)}편: " + ", ".join(str(h) for h in hits))
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=REQUEST_TIMEOUT * 18,  # 장문 생성이라 넉넉하게
            )

            if resp.status_code == 429:
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
                    wait = wait_seconds(resp.text, attempt)
                    log.warning(f"RPM 초과(429), {wait:.0f}초 후 재시도 ({attempt + 1}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue
                return ""

            if resp.status_code == 404:
                raise ModelExhausted(f"모델 사용 불가(404): {resp.text[:150]}")

            if resp.status_code in RETRYABLE_STATUS:
                if attempt < MAX_RETRIES:
                    wait = _backoff(attempt)
                    log.warning(
                        f"서버 일시 장애({resp.status_code}), {wait:.0f}초 후 재시도 "
                        f"({attempt + 1}/{MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue
                # 같은 모델이 MAX_RETRIES 번 연속 5xx면 일시 장애가 아니다.
                # 무료 티어 한도가 소진되면 구글은 429 대신 503을 섞어 내보낸다.
                # 여기서 빈 문자열을 돌려주면 write_all 이 '기사 실패'로 보고
                # 폴백 모델을 써보지도 않은 채 그 기사를 버린다.
                # (실측 2026-08-14: 죽은 모델에 90초씩 매달리다 기사 2건 유실.
                #  같은 실행에서 429를 받은 3번째 기사만 폴백으로 넘어가 성공)
                raise TransientModelFailure(
                    f"{resp.status_code} {MAX_RETRIES + 1}회 연속 — 모델 상태 이상"
                )

            if not resp.ok:
                log.warning(f"Gemini 응답 오류 [{resp.status_code}]: {resp.text[:200]}")
                return ""

            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            # 마크다운 잔재 제거 → 챕터 번호 부여 → 해요체 교정 → CTA 부착
            result = postprocess(text)
            if result.changes:
                log.info(f"  후처리 {result.total_fixes}건: " + "; ".join(result.changes[:3]))
            return result.text

        except ModelExhausted:
            raise
        except _NETWORK_ERRORS as e:
            # 응답 코드가 없는 실패. 남은 재시도를 반드시 쓴다.
            if attempt < MAX_RETRIES:
                wait = _backoff(attempt)
                log.warning(
                    f"통신 오류({type(e).__name__}), {wait:.0f}초 후 재시도 "
                    f"({attempt + 1}/{MAX_RETRIES})"
                )
                time.sleep(wait)
                continue
            # 5xx 와 같은 판단. 기사를 버리기 전에 다른 모델을 한 번 써본다.
            raise TransientModelFailure(f"통신 오류 {MAX_RETRIES + 1}회 연속: {e}")
        except (KeyError, IndexError) as e:
            log.warning(f"응답 파싱 실패: {e}")
            return ""
        except Exception as e:
            log.warning(f"글 생성 실패: {e}")
            return ""
    return ""


def _length_summary(markdown: str, item: dict) -> str:
    """초안 분량 로그 문구. 마크다운 길이와 순수 본문 길이를 함께 보여준다.

    목표 분량(1,700~1,900자 등)은 순수 본문(prose_chars) 기준이다. 마크다운
    길이만 찍으면 줄바꿈·표·제목까지 세어 초과로 오인하기 쉽다.
    (2026-09-23: '2,321자'로 찍힌 초안의 순수 본문은 1,966자였다)
    편별 값은 metrics.jsonl 에도 남지만 Actions 에서는 러너와 함께 사라지므로
    로그 한 줄로 확인할 수 있게 한다.
    """
    body = prose_chars(markdown)
    text = f"마크다운 {len(markdown):,}자 · 본문 {body:,}자"
    brief = item.get("brief")
    if brief is None:
        return text
    lo, hi, _ = brief.target_length()
    if body > hi:
        verdict = f"{body - hi:,}자 초과"
    elif body < lo:
        verdict = f"{lo - body:,}자 부족"
    else:
        verdict = "범위 안"
    return f"{text} / 목표 {lo:,}~{hi:,}자 → {verdict}"


def write_all(items: list[dict]) -> list[dict]:
    """승인 항목들에 블로그 원고를 채운다.

    items: [{page_id, title, url, subtopics, body}]
    반환: markdown 이 채워진 항목만
    """
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY 가 없습니다. .env 를 확인하세요.")
        return []

    # 블로그 작성은 긴 원문 파악과 표현력이 중요하므로 Flash를 우선 사용한다
    # (소주제 생성과 다른 체인 — RPD가 빡빡한 Flash를 글 작성에 아껴 쓰는 구조)
    chain = build_chain(GEMINI_WRITER_MODEL, GEMINI_WRITER_FALLBACK_MODELS)
    # 오늘 이미 한도가 끝난 모델은 건너뛴다. 매 실행 체인 앞부터 들이받으면
    # 죽은 모델에 90초씩 헌납하고 그 사이 다른 모델의 한도까지 갉아먹는다.
    chain = usable_chain(chain)
    log.info(f"{len(items)}건 블로그 초안 작성 시작 (모델: {chain[0]}, 폴백 {len(chain) - 1}개)")

    # 과거 글 코퍼스는 실행당 한 번만 읽는다. BM25 인덱스 구축이 비싸다.
    # 없거나 비어 있어도 진행한다 — 가이드 전문만으로도 초안은 나온다.
    try:
        corpus = BlogCorpus.load()
        log.info(f"과거 글 코퍼스 {len(corpus)}편 로드")
    except Exception as e:
        log.warning(f"코퍼스 로드 실패, 문체 예시 없이 진행: {e}")
        corpus = None

    result: list[dict] = []
    model_idx = 0
    first = True

    for item in items:
        if model_idx >= len(chain):
            break
        markdown = ""
        while model_idx < len(chain):
            if not first:
                time.sleep(REQUEST_INTERVAL)
            first = False
            try:
                markdown = _generate(item, chain[model_idx], corpus)
                break
            except ModelExhausted as e:
                log.warning(f"모델 '{chain[model_idx]}' 사용 불가 → 전환. {e}")
                # 일시 장애는 기록하지 않는다. 구글 쪽이 잠깐 흔들린 것까지
                # 소진으로 남기면 멀쩡한 모델이 하루 종일 차단된다.
                if not isinstance(e, TransientModelFailure):
                    mark_exhausted(chain[model_idx], str(e))
                model_idx += 1
                if model_idx < len(chain):
                    log.info(f"폴백 모델로 전환: {chain[model_idx]}")

        if model_idx >= len(chain):
            log.error(
                f"모든 모델({len(chain)}개)의 한도가 소진되어 중단 "
                f"({len(result)}/{len(items)}건 처리)"
            )
            break

        if not markdown:
            log.warning(f"초안 생성 실패, 건너뜀: {item['title'][:40]}")
            continue

        item["markdown"] = markdown
        item["model"] = chain[model_idx]  # 어느 모델이 썼는지 기록 (검토 강도 판단용)
        result.append(item)
        first_line = markdown.split("\n", 1)[0].removeprefix("# ")
        log.info(f"초안 작성: {first_line[:50]} ({_length_summary(markdown, item)})")

    log.info(f"초안 작성 완료 {len(result)}/{len(items)}건")
    return result
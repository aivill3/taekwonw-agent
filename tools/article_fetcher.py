"""본문 추출. (구 url_batch_processor.py의 추출 로직 이식)

핵심:
  - requests + User-Agent 헤더로 직접 다운로드 (trafilatura.fetch_url 미사용)
  - guess_best_decode: 한글 글자 수 스코어링으로 euc-kr/cp949 인코딩 자동 보정
  - trafilatura.extract(favor_precision=True)로 본문 추출
    → 결과가 짧으면 기본 모드로 한 번 더 뽑아 긴 쪽을 쓴다 (같은 HTML 재사용)
  - extract_metadata로 언론사명(press)·발행일(published) 누락분 보완
"""
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests
import trafilatura

from config.settings import MAX_WORKERS, REQUEST_TIMEOUT
from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)

# 봇 차단 회피용 헤더.
# User-Agent만 보내면 실제 브라우저와 쉽게 구별돼 대형 언론사(연합뉴스 등)가
# 연결을 끊는다(ConnectionResetError). 브라우저가 실제로 보내는 헤더를 함께 넣는다.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# 연결 자체가 끊기는 오류는 일시적인 경우가 많아 재시도하면 대체로 성공한다
FETCH_RETRIES = 2        # 최초 시도 외 추가 시도 횟수
RETRY_BACKOFF = 2.0      # 재시도 대기(초): 2 → 4

# 정밀 추출 결과가 이 길이 미만이면 기본 모드로 한 번 더 뽑는다.
# favor_precision 은 확신이 낮은 블록을 버리는데, 사이트 구조에 따라 본문
# 전체가 버려지고 저작권 줄만 남는다.
# (2026-09-21: 국기원 타지키스탄 9단증 기사 2건이 38자만 나와, 그 38자가
#  저작권 문구여서 정제 후 0자로 제외됐다. 6개 매체가 보도한 사건이었다)
# 값은 ranking_agent.MIN_BODY_CHARS 와 같은 250. 어차피 그 아래는 탈락하므로
# 더 낮게 잡을 이유가 없고, 더 높이면 멀쩡한 짧은 기사까지 두 번 파싱한다.
RETRY_EXTRACT_UNDER = 250


def _guess_best_decode(data: bytes, encodings: list[str]) -> str:
    """한글 글자 수가 가장 많은 디코딩 결과를 선택 (국내 언론사 euc-kr/cp949 대응)."""
    best_text: str | None = None
    best_score = -1
    for enc in encodings:
        if not enc:
            continue
        try:
            text = data.decode(enc, errors="replace")
        except LookupError:
            continue
        score = sum(0xAC00 <= ord(ch) <= 0xD7A3 for ch in text)
        if score > best_score:
            best_text, best_score = text, score
        if score > 10:  # 충분히 한글이 살아있으면 조기 종료
            break
    return best_text if best_text is not None else data.decode("utf-8", errors="replace")


def _headers_for(url: str) -> dict:
    """요청 헤더. Referer를 해당 사이트 자신으로 두어 정상 유입처럼 보이게 한다."""
    headers = dict(_HEADERS)
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            headers["Referer"] = f"{parts.scheme}://{parts.netloc}/"
    except ValueError:
        pass
    return headers


# 재시도할 가치가 있는 오류: 연결이 끊기거나 타임아웃된 경우.
# 404/403 같은 응답 코드 오류는 다시 시도해도 결과가 같으므로 제외한다.
_RETRYABLE_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _fetch_html(url: str) -> str | None:
    session = requests.Session()  # 연결 재사용으로 핸드셰이크 부담을 줄인다
    try:
        for attempt in range(FETCH_RETRIES + 1):
            try:
                resp = session.get(
                    url, headers=_headers_for(url), timeout=REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                candidates = [
                    resp.encoding, resp.apparent_encoding, "utf-8", "euc-kr", "cp949"
                ]
                return _guess_best_decode(resp.content, candidates)

            except _RETRYABLE_ERRORS as e:
                if attempt < FETCH_RETRIES:
                    wait = RETRY_BACKOFF * (attempt + 1)
                    log.info(
                        f"연결 오류, {wait:.0f}초 후 재시도 "
                        f"({attempt + 1}/{FETCH_RETRIES}): {url[:60]}"
                    )
                    time.sleep(wait)
                    continue
                log.warning(f"다운로드 실패(재시도 초과) ({url}): {e}")
                return None

            except Exception as e:
                log.warning(f"다운로드 실패 ({url}): {e}")
                return None
        return None
    finally:
        session.close()


def _extract(html: str, *, precise: bool) -> str:
    """본문 추출 한 번. precise=False 면 trafilatura 기본 모드(더 관대함).

    기본 모드는 캡션 줄까지 딸려 오지만, body_cleaner 가 뒤에서 걷어낸다.
    본문을 통째로 잃는 것보다 캡션이 섞이는 편이 낫다.
    """
    return trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        **({"favor_precision": True} if precise else {}),
    ) or ""


def _fetch_body(article: Article) -> Article:
    html = _fetch_html(article.url)
    if not html:
        return article
    try:
        body = _extract(html, precise=True)
        # 정밀 모드가 본문을 통째로 버린 경우를 구제한다. 받아 둔 HTML 을 다시
        # 파싱할 뿐이라 네트워크 요청은 늘지 않는다.
        if len(body) < RETRY_EXTRACT_UNDER:
            loose = _extract(html, precise=False)
            if len(loose) > len(body):
                log.info(
                    f"재추출 {len(body)}자 → {len(loose)}자: {article.title[:30]}"
                )
                body = loose
        article.body = body

        # 메타데이터로 누락 필드 보완 (네이버 API는 언론사명을 주지 않음)
        metadata = trafilatura.extract_metadata(html)
        if metadata:
            if not article.press and metadata.sitename:
                article.press = metadata.sitename
            if not article.published and metadata.date:
                article.published = str(metadata.date)
    except Exception as e:
        log.warning(f"추출 실패 ({article.url}): {e}")
    return article


def extract_all(articles: list[Article]) -> list[Article]:
    """병렬로 본문 추출. 본문을 확보한 기사만, 입력 순서 그대로 반환한다.

    as_completed 로 모으면 먼저 끝난 순서로 섞여 네이버 검색 순서가 깨진다.
    pool.map 은 병렬로 실행하면서 결과를 제출 순서대로 돌려준다.
    """
    log.info(f"{len(articles)}건 본문 추출 시작...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_fetch_body, articles))
    done = [a for a in results if a.body.strip()]
    log.info(f"본문 확보 {len(done)}/{len(articles)}건")
    return done
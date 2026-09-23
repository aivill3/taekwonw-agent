"""네이버 검색 API(뉴스) 수집기.

역할:
  1. 검색 키워드마다 API 호출 (API HUB 우선, 구 개발자센터 방식 폴백)
  2. 응답 원본을 data/raw/naver_<시각>_<키워드>.json 에 보존
  3. 공통 Article 스키마로 변환해, 네이버가 준 순서 그대로 반환
     (search_keyword, search_rank 에 어느 검색의 몇 위였는지 남긴다)

원본을 남기는 이유:
  검색 API 결과는 시점마다 달라진다. 며칠 뒤 같은 쿼리를 던져도 그날 그 30건은
  다시 안 나온다. 파이프라인 뒷단에서 이상이 보였을 때 대조할 원본이 없으면
  '수집이 잘못됐나, 처리가 잘못됐나'를 가릴 수 없다.

정렬
----
기본은 sim(관련도순)이다. 네이버 뉴스 검색 화면의 기본 정렬이라 사람이 검색했을
때 보는 순서와 가장 가깝다. 다만 화면에는 언론사 묶음·관련뉴스 표시가 섞여 있어
API 결과가 화면과 한 건 한 건 똑같다는 보장은 없다.

관련도순은 며칠 지난 기사를 계속 상위에 올린다. 예전에는 그래서 date(최신순)를
썼다. 지금은 date_filter(LOOKBACK_DAYS)와 state.processed_urls 가 오래된 기사와
이미 저장한 기사를 거르므로, 다시 받아와도 저장되지는 않는다.

유일한 수집원
------------
구글 뉴스 RSS 를 뺐으므로(2026-09-17) 네이버가 실패하면 수집할 것이 없다.
모든 키워드가 실패하면 NaverCollectError 를 던져 파이프라인을 세우고 Slack 으로
알린다. 빈 목록을 조용히 돌려주면 '새 기사 없음'과 구분되지 않는다.
일부 키워드만 실패하면 경고를 남기고 나머지로 진행한다.
"""
import html
import json
import os
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

from config.settings import (
    KST,
    NAVER_CLIENT_ID,
    NAVER_CLIENT_SECRET,
    RAW_DIR,
    REQUEST_TIMEOUT,
)
from config.collect_config import NAVER_DISPLAY, NAVER_SORT, SEARCH_KEYWORDS
from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)

# 네이버는 검색 API를 개발자센터에서 NAVER API HUB(네이버클라우드)로 옮겼다.
# 2026-07-31 신규 신청 종료, 2027-06-30 구 방식 지원 종료.
# 도메인·경로·인증 헤더가 모두 달라서 키만 바꿔서는 401 이 난다.
#
#   구 방식  openapi.naver.com/v1/search/news.json
#            X-Naver-Client-Id / X-Naver-Client-Secret
#   HUB      naverapihub.apigw.ntruss.com/search/v1/news
#            X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
#
# 응답 본문은 items 안의 다섯 필드(title, originallink, link, description,
# pubDate)가 그대로라 파서는 공용이다.
HUB_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
LEGACY_URL = "https://openapi.naver.com/v1/search/news.json"

# hub | legacy | auto
# auto 는 HUB 를 먼저 시도하고, 인증 오류(401/403)면 구 방식으로 한 번 더 시도한다.
# 어느 쪽 키인지 사용자가 신경 쓰지 않아도 되게 하기 위함이다.
NAVER_API_MODE = os.getenv("NAVER_API_MODE", "auto").lower()

# 네이버가 검색어에 <b> 태그를 씌워 돌려준다. 제목에 그대로 두면 Notion과
# 블로그 원고에까지 태그가 흘러간다.
_RE_TAG = re.compile(r"<[^>]+>")

# 파일명에 쓸 수 없는 문자. \w 는 한글을 포함한다.
_RE_UNSAFE = re.compile(r"[^\w]+")

_VALID_SORTS = ("sim", "date")


class NaverCollectError(RuntimeError):
    """네이버 수집이 전부 실패했다. 유일한 수집원이라 파이프라인을 세운다."""


def _sort() -> str:
    """설정된 정렬값. 오타면 관련도순으로 돌린다 (API 는 잘못된 값에 400 을 준다)."""
    sort = NAVER_SORT.strip().lower()
    if sort not in _VALID_SORTS:
        log.warning(f"NAVER_SORT='{NAVER_SORT}' 는 지원하지 않습니다. sim 으로 진행합니다")
        return "sim"
    return sort


def _clean(text: str) -> str:
    """HTML 태그와 엔티티를 제거한다 (&quot; &amp; &lt; 등)."""
    return html.unescape(_RE_TAG.sub("", text or "")).strip()


def _to_kst_iso(pub_date: str) -> str:
    """RFC 2822 형식(Mon, 10 Aug 2026 09:07:13 +0900) → KST ISO 8601."""
    if not pub_date:
        return ""
    try:
        return parsedate_to_datetime(pub_date).astimezone(KST).isoformat()
    except (TypeError, ValueError):
        return pub_date  # 파싱 실패 시 원본을 남긴다 (date_filter 가 다시 시도)


def _save_raw(items: list[dict], query: str) -> str:
    """API 응답 원본을 JSON 으로 남긴다. 실패는 수집을 막지 않는다.

    키워드를 파일명에 넣는다. 같은 초에 키워드 두 개를 연달아 호출하므로,
    시각만 쓰면 뒤 키워드가 앞 키워드 파일을 덮어쓴다.
    """
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    slug = _RE_UNSAFE.sub("_", query).strip("_") or "query"
    path = RAW_DIR / f"naver_{stamp}_{slug}.json"
    try:
        path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning(f"raw 저장 실패: {e}")
        return ""
    return path.name


def _call(
    url: str, headers: dict, query: str, display: int, sort: str
) -> requests.Response:
    """검색 요청 한 번. 파라미터는 두 방식이 같다."""
    return requests.get(
        url,
        headers=headers,
        params={
            "query": query,
            "display": min(max(display, 1), 100),  # API 상한 100
            "start": 1,
            "sort": sort,
        },
        timeout=REQUEST_TIMEOUT,
    )


def _endpoints() -> list[tuple[str, str, dict]]:
    """시도할 (이름, URL, 헤더) 목록. NAVER_API_MODE 에 따라 순서가 정해진다."""
    hub = (
        "API HUB",
        HUB_URL,
        {
            "X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID,
            "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET,
        },
    )
    legacy = (
        "개발자센터(구)",
        LEGACY_URL,
        {
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        },
    )
    if NAVER_API_MODE == "hub":
        return [hub]
    if NAVER_API_MODE == "legacy":
        return [legacy]
    return [hub, legacy]  # auto


def collect(query: str, display: int = NAVER_DISPLAY) -> list[Article] | None:
    """키워드 하나의 검색 결과를 네이버 응답 순서대로 Article 목록으로 반환한다.

    호출에 실패하면 None 을 돌려준다. 검색 결과가 0건인 경우(빈 목록)와
    구분해야 collect_all() 이 '전부 실패'를 판정할 수 있다.
    """
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        log.warning("NAVER_CLIENT_ID/SECRET 이 없어 네이버 수집을 건너뜁니다")
        return None

    sort = _sort()
    items: list[dict] | None = None
    for name, url, headers in _endpoints():
        try:
            resp = _call(url, headers, query, display, sort)
            if resp.status_code in (401, 403):
                # 다른 방식의 키일 수 있다. 남은 후보가 있으면 그쪽을 시도한다.
                log.warning(f"네이버 {name} 인증 실패({resp.status_code})")
                continue
            resp.raise_for_status()
            items = resp.json().get("items", [])
            log.info(f"네이버 {name} 방식으로 호출 ('{query}')")
            break
        except Exception as e:
            log.warning(f"네이버 {name} 호출 실패 ('{query}'): {e}")
            continue

    if items is None:
        log.warning(f"네이버 '{query}' 수집 실패")
        return None

    raw_name = _save_raw(items, query)

    articles: list[Article] = []
    # 순위는 URL 이 없는 항목을 건너뛰기 전에 매긴다.
    # 네이버 응답에서의 실제 위치를 남겨야 raw 파일과 대조할 수 있다.
    for rank, it in enumerate(items, 1):
        # originallink 는 언론사 원문, link 는 네이버 뉴스 페이지다.
        # 원문을 우선한다 — 본문 추출이 더 잘 되고, 다른 키워드로 들어온
        # 같은 기사와 URL 이 일치해 중복 판정에도 걸린다.
        url = (it.get("originallink") or it.get("link") or "").strip()
        if not url:
            continue
        articles.append(
            Article(
                title=_clean(it.get("title", "")),
                url=url,
                source="naver",
                published=_to_kst_iso(it.get("pubDate", "")),
                summary=_clean(it.get("description", "")),
                search_keyword=query,
                search_rank=rank,
            )
        )

    suffix = f" (raw: {raw_name})" if raw_name else ""
    log.info(f"'{query}' {len(articles)}건 수집 (정렬={sort}){suffix}")
    return articles


def collect_all(
    keywords: list[str] | None = None,
    display: int = NAVER_DISPLAY,
) -> list[Article]:
    """모든 검색 키워드를 차례로 수집해 이어 붙인다.

    순서: 키워드 순서 → 키워드 안에서는 네이버 응답 순서.
    중복은 여기서 거르지 않는다. collect_workflow 가 dedupe() 로 한 번에
    거르며, 먼저 온 쪽(앞 키워드 · 상위 순위)이 남는다.

    모든 키워드가 실패하면 NaverCollectError 를 던진다.
    """
    keywords = keywords if keywords is not None else SEARCH_KEYWORDS
    if not keywords:
        raise NaverCollectError("검색 키워드가 없습니다 (SEARCH_KEYWORDS 확인)")
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        raise NaverCollectError("NAVER_CLIENT_ID/SECRET 이 없어 수집할 수 없습니다")

    articles: list[Article] = []
    failed: list[str] = []
    for kw in keywords:
        got = collect(kw, display)
        if got is None:
            failed.append(kw)
            continue
        articles.extend(got)

    if len(failed) == len(keywords):
        raise NaverCollectError(
            "모든 키워드의 네이버 수집이 실패했습니다 — 키와 방식을 확인하세요. "
            "API HUB 키라면 NAVER_API_MODE=hub 를 두면 재시도 없이 바로 붙습니다"
        )
    if failed:
        log.warning(f"일부 키워드 수집 실패, 나머지로 진행합니다: {', '.join(failed)}")

    log.info(
        f"키워드 {len(keywords) - len(failed)}/{len(keywords)}개 수집 · "
        f"합계 {len(articles)}건 (키워드 간 중복 포함)"
    )
    return articles
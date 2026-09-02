"""네이버 검색 API(뉴스) 수집기.

역할:
  1. 검색 API 호출 (API HUB 우선, 구 개발자센터 방식 폴백)
  2. 응답 원본을 data/raw/naver_*.json 에 보존
  3. 공통 Article 스키마로 변환해 반환

원본을 남기는 이유:
  검색 API 결과는 시점마다 달라진다. 며칠 뒤 같은 쿼리를 던져도 그날 그 30건은
  다시 안 나온다. 파이프라인 뒷단에서 이상이 보였을 때 대조할 원본이 없으면
  '수집이 잘못됐나, 처리가 잘못됐나'를 가릴 수 없다.

정렬은 date(최신순)를 쓴다. sim(정확도순)은 오래된 기사를 계속 상위에 올려
매 실행 같은 기사를 다시 가져온다.
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
from config.collect_config import NAVER_DISPLAY, QUERY
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


def _save_raw(items: list[dict]) -> str:
    """API 응답 원본을 JSON 으로 남긴다. 실패는 수집을 막지 않는다."""
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    path = RAW_DIR / f"naver_{stamp}.json"
    try:
        path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning(f"raw 저장 실패: {e}")
        return ""
    return path.name


def _call(url: str, headers: dict, query: str, display: int) -> requests.Response:
    """검색 요청 한 번. 파라미터는 두 방식이 같다."""
    return requests.get(
        url,
        headers=headers,
        params={
            "query": query,
            "display": min(max(display, 1), 100),  # API 상한 100
            "start": 1,
            "sort": "date",
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


def collect(query: str = QUERY, display: int = NAVER_DISPLAY) -> list[Article]:
    """네이버 뉴스 검색 결과를 Article 목록으로 반환한다.

    키가 없거나 호출이 실패하면 빈 목록을 돌려준다. 구글 RSS 만으로도
    파이프라인은 돌아가므로, 한쪽 소스의 장애로 전체를 세우지 않는다.
    """
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        log.warning("NAVER_CLIENT_ID/SECRET 이 없어 네이버 수집을 건너뜁니다")
        return []

    items: list[dict] | None = None
    for name, url, headers in _endpoints():
        try:
            resp = _call(url, headers, query, display)
            if resp.status_code in (401, 403):
                # 다른 방식의 키일 수 있다. 남은 후보가 있으면 그쪽을 시도한다.
                log.warning(f"네이버 {name} 인증 실패({resp.status_code})")
                continue
            resp.raise_for_status()
            items = resp.json().get("items", [])
            log.info(f"네이버 {name} 방식으로 호출")
            break
        except Exception as e:
            log.warning(f"네이버 {name} 호출 실패: {e}")
            continue

    if items is None:
        log.warning(
            "네이버 수집 실패 — 키와 방식을 확인하세요. "
            "API HUB 키라면 .env 에 NAVER_API_MODE=hub 를 두면 재시도 없이 바로 붙습니다"
        )
        return []

    raw_name = _save_raw(items)

    articles: list[Article] = []
    for it in items:
        # originallink 는 언론사 원문, link 는 네이버 뉴스 페이지다.
        # 원문을 우선한다 — 본문 추출이 더 잘 되고, 구글에서 온 같은 기사와
        # URL 이 일치해 중복 판정에도 걸린다.
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
            )
        )

    suffix = f" (raw: {raw_name})" if raw_name else ""
    log.info(f"{len(articles)}건 수집{suffix}")
    return articles

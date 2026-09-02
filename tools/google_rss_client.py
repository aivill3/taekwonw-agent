"""구글 뉴스 RSS 수집기.
(구 google_news_fetch.py + googlenewsdc_threadpe.py 통합)

역할:
  1. feedparser로 RSS 파싱
  2. googlenewsdecoder + ThreadPoolExecutor로 구글 리다이렉트 링크를
     언론사 원문 URL로 병렬 디코딩
  3. 디코딩 성공 건만 Article 리스트로 반환

수집 원본은 data/raw/google_*.json 에 남긴다 (naver_news_client 와 같은 규약).
RSS 원문 XML이 아니라 인코딩 URL → 언론사 URL 매핑을 남기는데, 나중에 조사할 때
궁금한 것은 XML이 아니라 '그 링크가 어디로 풀렸는가'이기 때문이다.
중간 CSV는 만들지 않는다 (정제 전 백업은 collect_workflow 의 save_csv 담당).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json

import feedparser
from googlenewsdecoder import gnewsdecoder

from config.settings import DECODER_INTERVAL, KST, MAX_WORKERS, RAW_DIR
from config.collect_config import GOOGLE_RSS_URL
from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)


def _to_kst_iso(entry) -> str:
    """RSS 발행일(UTC)을 KST 기준 ISO 8601로 변환.
    feedparser의 published_parsed는 UTC struct_time으로 정규화되어 있다.
    예) 'Mon, 27 Jul 2026 07:56:00 GMT' → '2026-07-27T16:56:00+09:00'"""
    parsed = entry.get("published_parsed")
    if parsed:
        dt = datetime(*parsed[:6], tzinfo=timezone.utc)
        return dt.astimezone(KST).isoformat()
    return entry.get("published", "")  # 파싱 불가 시 원본 문자열 유지


def _decode(google_url: str) -> str | None:
    """구글 뉴스 링크 → 언론사 원문 URL. 실패 시 None."""
    try:
        result = gnewsdecoder(google_url, interval=DECODER_INTERVAL)
        if result.get("status"):
            return result["decoded_url"]
        log.warning(f"decode 실패: {result.get('message', 'Unknown error')}")
    except Exception as e:  # 디코딩 실패는 해당 기사만 건너뜀
        log.warning(f"decode 오류: {e}")
    return None


def _save_raw(entries: list, articles: list[Article]) -> str:
    """수집 원본을 JSON으로 남긴다 (naver_news_client 와 같은 규약).

    RSS 원문 XML이 아니라 '디코딩 결과'를 남긴다. 구글이 주는
    news.google.com/rss/articles/CBMi... 인코딩 문자열은 나중에 열어봐도
    쓸 데가 없고, 실제로 궁금한 것은 그게 어느 언론사로 풀렸는가이기 때문이다.
    디코딩은 차단 방지 간격 때문에 100건에 90초 가까이 걸리는 구간이라,
    결과를 남겨두면 조사할 때 다시 돌릴 필요가 없다.

    디코딩에 실패한 항목도 decoded=null 로 함께 남긴다. 실패가 특정 매체에
    몰리는지 보려면 성공분만으로는 알 수 없다.
    """
    decoded_by_link = {a.title: a.url for a in articles}
    records = []
    for e in entries:
        title = e.get("title", "")
        records.append({
            "title": title,
            "google_url": e.get("link", ""),
            "decoded_url": decoded_by_link.get(title),
            "press": e.get("source", {}).get("title", ""),
            "published": _to_kst_iso(e),
        })

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    path = RAW_DIR / f"google_{stamp}.json"
    try:
        path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        # 백업 실패가 수집을 막을 이유는 없다
        log.warning(f"raw 저장 실패: {e}")
        return ""
    return path.name


def collect(rss_url: str = GOOGLE_RSS_URL) -> list[Article]:
    feed = feedparser.parse(rss_url)
    entries = feed.entries
    log.info(f"RSS 항목 {len(entries)}건, URL 디코딩 시작...")

    articles: list[Article] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {pool.submit(_decode, e.link): e for e in entries}
        for future in as_completed(future_map):
            entry = future_map[future]
            decoded = future.result()
            if not decoded:
                continue
            articles.append(
                Article(
                    title=entry.get("title", ""),
                    url=decoded,
                    source="google_rss",
                    press=entry.get("source", {}).get("title", ""),
                    published=_to_kst_iso(entry),
                    summary=entry.get("summary", ""),
                )
            )

    raw_name = _save_raw(entries, articles)
    suffix = f" (raw: {raw_name})" if raw_name else ""
    log.info(f"디코딩 성공 {len(articles)}/{len(entries)}건{suffix}")
    return articles

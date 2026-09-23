"""기사 데이터 모델과 중복 제거.

파이프라인 전 구간이 이 Article 하나를 들고 다닌다. 단계마다 필드가 채워진다.

    수집    title, url, source, press, published, summary,
            search_keyword, search_rank         (tools/naver_news_client)
    추출    body                      (tools/article_fetcher)
    정제    body_clean                (agents/collecting/body_cleaner)
    저장    page_id                   (tools/notion_store)
    랭킹    keyword_score, matched_keywords, score_norm, report_count
    소주제  subtopics                 (agents/subtopic)

body 와 body_clean 을 둘 다 남기는 이유:
  정제는 되돌릴 수 없다. 규칙이 본문을 과하게 지웠을 때 원본과 대조할 수 없으면
  왜 말랐는지 알 방법이 없다. body_cleaner.diagnose() 가 body 를 다시 훑는다.

모든 필드에 기본값이 있다. 단계가 건너뛰어져도 AttributeError 로 파이프라인이
멈추지 않게 하기 위해서다 — 값이 비어 있는 것과 필드가 없는 것은 다르다.
"""
from dataclasses import dataclass, field, asdict
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from core.logger import get_logger

log = get_logger(__name__)

# 추적용 쿼리 파라미터. 같은 기사인데 URL이 달라 보이게 만드는 주범이라
# 중복 판정 전에 떼어낸다.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "spm", "ref", "referer", "referrer",
    "from", "sid", "cid", "s_kwcid", "n_media", "n_query", "n_rank",
}


@dataclass
class Article:
    # ── 수집 단계 ──
    title: str = ""
    url: str = ""
    source: str = ""          # "naver" | "manual"
    press: str = ""           # 언론사명
    published: str = ""       # ISO 8601 (KST)
    summary: str = ""         # API가 준 요약. 본문 추출 실패 시의 보험
    search_keyword: str = ""  # 이 기사를 찾은 검색 키워드 (중복이면 앞 키워드)
    search_rank: int = 0      # 그 키워드 검색 결과에서의 순위 (1부터, 네이버 응답 순서)

    # ── 본문 ──
    body: str = ""            # 추출 원본 (정제 전)
    body_clean: str = ""      # 정제 결과. 이후 단계는 이것만 본다

    # ── 저장 ──
    page_id: str = ""         # Notion 페이지 id

    # ── 랭킹 ──
    keyword_score: float = 0.0        # 트렌드 키워드 기반 원점수
    matched_keywords: list[str] = field(default_factory=list)
    score_norm: int = 0               # 1위를 100으로 둔 정규화 점수
    report_count: int = 1             # 같은 사건을 보도한 매체 수

    # ── 소주제 ──
    subtopics: list[str] = field(default_factory=list)

    @property
    def date(self) -> str:
        """발행일(YYYY-MM-DD). 프롬프트에 넣을 때 시각까지는 필요 없다."""
        return self.published[:10] if self.published else ""

    @property
    def matched_keyword(self) -> str:
        """대표 키워드 1개. SEO 검사와 프롬프트가 참조한다."""
        return self.matched_keywords[0] if self.matched_keywords else ""

    def to_dict(self) -> dict:
        """CSV/JSON 직렬화용. collect_workflow.save_csv 가 열 순서로 쓴다."""
        return asdict(self)


def canonical_url(url: str) -> str:
    """중복 판정용으로 URL을 정규화한다.

    같은 기사가 네이버 경유·구글 디코딩 경유로 각각 들어오면서 쿼리스트링이나
    끝 슬래시만 다른 경우가 흔하다. 그대로 두면 다른 기사로 보고 두 번 저장된다.
    (실측: 같은 기사가 Notion에 page_id 만 다른 채 두 건 생성)

    스킴과 www 는 유지한다. 다른 사이트를 같은 것으로 합치는 위험보다
    같은 것을 둘로 세는 편이 덜 나쁘기 때문이다.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.netloc:
        return url.strip()

    query = urlencode([
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _title_key(title: str) -> str:
    """제목 기반 보조 키. 공백·기호를 지우고 매체명 접미사를 뗀다.

    구글 RSS는 제목 끝에 ' - 매체명'을 붙여 준다. 네이버에서 온 같은 기사와
    URL이 다르면 URL만으로는 못 잡으므로 제목으로 한 번 더 거른다.
    """
    t = title.rsplit(" - ", 1)[0] if " - " in title else title
    return "".join(ch for ch in t if ch.isalnum())


def dedupe(articles: list[Article]) -> list[Article]:
    """URL 정규화 + 제목으로 중복 기사를 제거한다. 먼저 온 것을 남긴다.

    먼저 온 쪽을 남기는 이유: naver_news_client.collect_all() 은 키워드 순서,
    그 안에서는 검색 순위 순서로 이어 붙인다. 먼저 온 쪽이 앞 키워드·상위
    순위이므로, 검색 화면의 순서와 키워드 우선순위가 그대로 지켜진다.
    """
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    kept: list[Article] = []
    dropped = 0

    for a in articles:
        key_url = canonical_url(a.url)
        key_title = _title_key(a.title)
        if key_url in seen_url or (key_title and key_title in seen_title):
            dropped += 1
            continue
        seen_url.add(key_url)
        if key_title:
            seen_title.add(key_title)
        kept.append(a)

    if dropped:
        log.info(f"중복 {dropped}건 제거")
    return kept
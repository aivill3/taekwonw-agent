"""뉴스 자동 파이프라인 — 수집부터 초안 저장까지 사람 개입 없이 한 번에 돈다.

흐름
----
  상태 로드 → 네이버 수집(레인별 · 검색 순서) → 날짜 필터 → 처리 이력 제외
  → 본문 추출 · 정제
  → 주제 판정 (문장 임베딩, CLASSIFY_LANE 레인의 후보만 좁힌다)
  → [레인별] 선정 (보도 매체 수 · 주제 적합성 · 같은 사건 제거, 레인 간 포함)
  → 소주제 생성 (LLM #1, 모든 레인분을 한 번에 배치)
  → [레인별] 4건씩 묶기
  → 초안 작성 (LLM #2) → Content DB 저장 → 삽화 → 로컬 저장
  → 처리 이력 기록

뉴스 DB 를 쓰지 않는다 (2026-09-17)
---------------------------------
예전에는 수집 기사를 뉴스 DB 보드에 모두 올리고, 사람이 칸에 배치한 뒤
[초안 작성] 버튼으로 넘겼다 (content → confirm → poll → publish).
승인 게이트를 없애면서 그 보드도 없앴다. 결과는 Content DB 한 곳에서 본다.

  검색키워드  필터·그룹 기준 (키워드마다 뷰를 따로 둔다)
  수집회차    정렬 1순위, 내림차순 (최근 실행이 위)
  검색순위    정렬 2순위, 오름차순 (네이버 검색 순서)

원문 기사는 Content 페이지 안의 토글에 들어간다.

키워드를 따로 보는 방식
--------------------
키워드마다 후보를 따로 만들고, 따로 고르고, 따로 묶는다. 한 글에 두
키워드의 기사가 섞이지 않는다.

같은 기사가 두 키워드 검색에 모두 걸리면 두 후보 목록에 각각 들어간다
(검색 순위도 키워드마다 다르다). 앞 키워드가 먼저 고르고, 뒤 키워드는 앞에서
이미 고른 것을 빼고 고른다. 빼는 기준은 두 가지다.

  같은 기사   URL 또는 제목이 같다
  같은 사건   사건 시그니처가 ranking_agent.same_event() 기준을 넘는다
              (매체만 다른 같은 행사 기사. 실측 2026-09-18: 춘천 평화걷기가
               두 키워드에 모두 올라와 같은 소재로 두 편이 나갈 뻔했다)

처리 이력(state.processed_urls)
-----------------------------
남기는 것 — 다음 실행에서 다시 보지 않는다
  - 초안 저장까지 끝난 글의 기사, 그 기사와 같은 사건으로 합쳐진 기사
  - 주제 부적합 기사 (기사 하나만 보고 판정하므로 다음에도 결과가 같다)
  - 초안에 쓴 기사의 사건 시그니처 (state.written_events, 레인 조회일수 최댓값만큼)
    URL 이력은 '그 기사'만 막는다. 다음 회차에 다른 매체가 같은 사건을 쓰면
    새 URL 이라 다시 후보가 된다. 시그니처로 '그 사건'을 막는다.
    (실측 2026-09-23: 오전 조직 글의 '국제대회 3종 춘천 유치'가 다른 매체
     기사로 오후 조직 레인 후보에 다시 올라왔다)
남기지 않는 것 — LOOKBACK_DAYS 안에서 다음 실행의 후보가 된다
  - 선정되지 않은 기사, 4건을 못 채워 보류된 기사
  - 소주제·초안 생성이나 Content 저장이 실패한 기사
  - 본문 추출·정제에 실패한 기사

한 번에 쓰는 양
--------------
레인(발행 카테고리)당 POSTS_PER_KEYWORD 편 (기본 1편 = 기사 4건).
기본값이면 한 실행에 소주제 배치 호출 + 초안 최대 레인 수만큼(지금 3회)이다.

실행 옵션
--------
  dry_run          선정·묶기까지만 보고 끝낸다. LLM·Notion·상태를 건드리지 않는다.
  skip_subtopic    위와 같지만 CSV 백업은 남긴다 (--no-subtopic).
  no_notion        초안까지 만들어 콘솔에 출력한다. Notion·상태는 건드리지 않는다.
  skip_duplicates  False 면 처리 이력을 무시하고 다시 고른다 (재실행 확인용).
  with_images      False 면 삽화를 만들지 않는다 (나중에 `run.py images`).
  with_quality     False 면 품질 측정을 건너뛴다.

에러 처리 정책
------------
  - 기사 단위 오류(다운로드/추출/정제 실패) → 해당 기사만 건너뛴다.
  - 묶음 단위 오류(초안·저장 실패) → 그 묶음만 건너뛰고 이력에 남기지 않는다.
  - 중단급 오류(네이버 전부 실패, Content DB 접근 불가, 환경변수 누락)
    → ERROR 로그 + Slack 알림 후 중단.
"""
import csv
import os
from collections import Counter
from dataclasses import replace
from datetime import datetime

from core import state_store as state
from agents.collecting.body_cleaner import clean_all
from tools import naver_news_client
from config.collect_config import LANES, POSTS_PER_KEYWORD
from config.settings import KST, PROCESSED_DIR
from agents.collecting.date_filter import filter_since, latest_published
from tools.article_fetcher import extract_all
from core.logger import get_logger, setup
from core.article_models import Article, canonical_url, dedupe
from tools.slack_notifier import notify_empty, notify_failure
from agents.drafting.article_grouper import BUNDLE_SIZE, format_groups, group_articles
from agents.drafting.draft_prompt import SourceArticle
from agents.drafting.schema_draft_agent import BODY_LIMIT
from agents.ranking.ranking_agent import (
    build_signatures,
    is_on_topic,
    rank,
    same_event,
    threshold_summary,
)
from agents.subtopic.subtopic_agent import generate_all
from agents.topic.topic_classifier import ORG, classify
from workflows.publish_workflow import publish_bundles

log = get_logger(__name__)

# 주제 판정이 레인 배정을 넘겨받을 레인 이름. 빈 값이면 판정을 끄고
# 지금까지의 검색 키워드 배정을 그대로 쓴다 (되돌리는 손잡이).
#
# 한 레인만 넘기는 이유: 판정기는 '조직이냐 아니냐'를 96.6% 로 가르지만
# '대회냐 태권도냐'는 79% 에 그친다. 두 범주가 실제로 겹쳐서 생기는
# 한계라 모델을 바꿔도 안 넘는다. 잘하는 축만 맡긴다.
#
# 왜 필요한가 (2026-09-22 실측, 수집 143건):
#   국기원         13건 중 10건이 조직 기사   77%
#   세계태권도연맹  64건 중 14건              22%
#   대한태권도협회  30건 중  4건              13%
# 뒤의 둘은 조직이 '주최자'로만 언급된다. 키워드를 바꿔도 조직명이
# 기사에 나오기만 하면 걸리므로 키워드로는 고칠 수 없다.
CLASSIFY_LANE = os.getenv("CLASSIFY_LANE", "조직").strip()


def _norm(url: str) -> str:
    """처리 이력용 URL 정규화. state_store 가 받아 온 규칙 그대로다."""
    return url.strip().rstrip("/")


def save_csv(articles: list[Article], prefix: str = "articles") -> None:
    """정제 완료본을 CSV로 백업 (Notion 장애 시 재처리용).

    한 실행에서 두 번 부른다.
      collected_* : 정제 직후 수집분 전체. Notion 저장이 실패해도 그날 결과가 남는다.
      selected_*  : 선정분(소주제 포함). 어떤 기사가 왜 뽑혔는지 확인용.

    prefix 로 구분하지 않으면 같은 실행의 두 파일이 타임스탬프만 다른 채
    섞여 어느 쪽이 전체인지 알 수 없다.
    """
    if not articles:
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = PROCESSED_DIR / f"{prefix}_{stamp}.csv"
    fields = list(articles[0].to_dict().keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for a in articles:
            row = a.to_dict()
            # 리스트 필드(subtopics)는 CSV에 담기 위해 개행으로 합친다
            row["subtopics"] = "\n".join(row.get("subtopics") or [])
            writer.writerow(row)
    log.info(f"CSV 백업: {path.name}")


def _split_by_lane(articles: list[Article]) -> dict[str, list[Article]]:
    """레인별 후보 목록. 각 목록 안에서만 중복을 거른다 (먼저 온 = 상위 순위).

    한 레인에 키워드가 여럿이면 키워드 순서대로 이어 붙인 뒤 중복을 거른다.
    같은 기사가 국기원과 태권도협회에 모두 걸리면 앞 키워드 쪽이 남는다.
    """
    lanes: dict[str, list[Article]] = {}
    for lane in LANES:
        merged = dedupe([a for a in articles if a.search_keyword in lane.keywords])
        lanes[lane.name] = merged
        log.info(f"[{lane.name}] 후보 {len(merged)}건")
    return lanes


def _extract_once(lanes: dict[str, list[Article]]) -> dict[str, list[Article]]:
    """본문을 URL 당 한 번만 받아 각 키워드 목록에 나눠 준다.

    같은 기사가 두 키워드에 걸려도 다운로드는 한 번이다. 검색 위치는
    키워드마다 다르므로, 본문을 받은 기사를 복사해 위치만 바꿔 끼운다.
    """
    unique: dict[str, Article] = {}
    for lane in lanes.values():
        for a in lane:
            unique.setdefault(canonical_url(a.url), a)

    extracted = extract_all(list(unique.values()))
    # 정제 '전' 백업 — 정제에서 걸러진 기사도 원본을 남겨 원인을 조사할 수 있게 한다.
    save_csv(extracted, prefix="collected")
    fetched = clean_all(extracted)
    by_url = {canonical_url(a.url): a for a in fetched}

    out: dict[str, list[Article]] = {}
    for kw, lane in lanes.items():
        kept = []
        for a in lane:
            src = by_url.get(canonical_url(a.url))
            if src is None:
                continue
            kept.append(replace(
                src,
                search_keyword=a.search_keyword,
                search_rank=a.search_rank,
                matched_keywords=[],
                subtopics=[],
            ))
        out[kw] = kept
        log.info(f"[{kw}] 본문 확보 {len(kept)}/{len(lane)}건")
    return out


def _pool(lanes: dict[str, list[Article]]) -> list[Article]:
    """모든 레인의 후보를 URL 당 하나로 모은다. 사건 시그니처의 기준 집합이다.

    select_by_lane() 과 run() 이 같은 집합으로 시그니처를 계산해야 한다.
    시그니처는 문서 빈도로 일반어를 걸러 내므로, 집합이 다르면 같은 기사도
    토큰이 달라진다.
    """
    return list(
        {canonical_url(a.url): a for lane in lanes.values() for a in lane}.values()
    )


def select_by_lane(
    lanes: dict[str, list[Article]],
    per_post: int,
    *,
    known_events: list[set[str]] | None = None,
) -> tuple[dict[str, tuple[list[Article], list[list[Article]]]], list[str]]:
    """레인별 선정. 앞 레인이 고른 기사·사건은 뒤 레인 후보에서 뺀다.

    반환: ({레인 이름: (선정 목록, 선정분 각각의 사건 기사 목록)}, 주제 부적합 URL)

    known_events 는 이전 회차 초안에 쓴 사건의 시그니처다. 이와 닮은 기사는
    모든 레인에서 후보에서 뺀다. 주지 않으면(sweep.py) 이번 실행만 본다.

    run() 에서 떼어낸 이유: 이 단계는 네트워크·Notion·상태를 전혀 건드리지
    않는 순수 계산이다. 저장된 CSV 로 임계값을 바꿔 가며 결과를 비교하는
    tools/sweep.py 가 같은 함수를 부른다. 실험 도구가 이 로직을 따로
    베껴 쓰면 언젠가 어긋나고, 그러면 실험 결과를 믿을 수 없게 된다.
    """
    picks: dict[str, tuple[list[Article], list[list[Article]]]] = {}
    off_topic: list[str] = []
    taken_urls: set[str] = set()
    taken_titles: set[str] = set()
    taken_events: list[set[str]] = []
    known = list(known_events or [])

    # 사건 시그니처는 모든 레인의 후보를 한 번에 놓고 계산한다.
    # 레인별로 따로 계산하면 기준(문서 빈도)이 달라져 레인 간 비교가 어긋난다.
    pooled = _pool(lanes)
    signatures = build_signatures(pooled)

    # 주제 판정. 레인 루프 밖에서 한 번만 돌린다 — 같은 기사가 여러 레인
    # 후보에 들어 있어도 판정은 하나여야 하고, 모델 로딩이 레인마다
    # 반복되면 그만큼 느려진다.
    #
    # 판정할 수 없으면(앵커 없음·torch 미설치·모델 내려받기 실패) 빈 목록이
    # 돌아오고, 아래에서 키워드 배정을 그대로 쓴다. 판정기 때문에 발행이
    # 멈추는 것이 판정기가 없는 것보다 나쁘다.
    #
    # 판정이 꺼지는 두 경로를 로그에 남긴다. 둘 다 발행은 계속되므로
    # 경고가 없으면 조직 레인에 대회 기사가 섞여도 원인을 찾을 수 없다.
    #   - CLASSIFY_LANE 이 레인 이름과 다르다 (레인 이름만 바꾼 경우)
    #   - classify() 가 빈 목록을 돌려준다 (앵커·torch·모델 없음)
    # 레인 이름이 안 맞으면 classify() 를 아예 부르지 않는다 — 쓰지도 않을
    # 결과를 위해 모델을 올리는 시간만 든다.
    verdicts: dict[str, str] = {}
    lane_names = [l.name for l in LANES]
    if CLASSIFY_LANE and CLASSIFY_LANE not in lane_names:
        log.warning(
            f"CLASSIFY_LANE='{CLASSIFY_LANE}' 인 레인이 없습니다 "
            f"({', '.join(lane_names)}) — 주제 판정을 건너뜁니다"
        )
    elif CLASSIFY_LANE:
        verdicts = dict(zip((canonical_url(a.url) for a in pooled), classify(pooled)))
        if verdicts:
            tally = Counter(verdicts.values())
            log.info("주제 판정 " + " · ".join(f"{k} {v}건" for k, v in tally.most_common()))
        else:
            log.warning(
                f"주제 판정 결과가 비었습니다 — [{CLASSIFY_LANE}] 레인을 "
                f"검색 키워드 배정으로 선정합니다"
            )

    for lane in LANES:
        kw = lane.name
        pool: list[Article] = []
        for a in lanes.get(kw, []):
            if canonical_url(a.url) in taken_urls or a.title in taken_titles:
                continue
            sig = signatures.get(a.url, set())
            if any(same_event(sig, s) for s in known):
                log.info(f"[{kw}] 이전 글에서 다룬 사건, 후보 제외: {a.title[:40]}")
                continue
            if any(same_event(sig, s) for s in taken_events):
                log.info(f"[{kw}] 앞 레인이 다룬 사건, 후보 제외: {a.title[:40]}")
                continue
            pool.append(a)

        # 이 레인만 검색 키워드가 아니라 기사 내용으로 후보를 정한다.
        # 판정 여유(1·2등 차이)로 거르지 않는 이유: 실측이 반대였다.
        # 착공식 기사들은 여유 0.02~0.04 인데 전부 조직 정답이었고
        # (한 기사에 착공식과 대회 개막이 같이 들어 있어 1·2등이 붙는다),
        # 여유 0.41~0.49 인 세 건은 전부 오판이었다.
        if kw == CLASSIFY_LANE and verdicts:
            before = len(pool)
            pool = [a for a in pool if verdicts.get(canonical_url(a.url)) == ORG]
            log.info(f"[{kw}] 주제 판정으로 후보 좁힘: {before}건 → {len(pool)}건")

        off_topic += [a.url for a in pool if not is_on_topic(a)[0]]
        log.info(f"── [{kw}] 선정 (후보 {len(pool)}건 → 최대 {per_post}건) ──")
        # 사건 묶기도 레인 간 제외와 같은 전체 시그니처로 한다 (rank() 주석 참고)
        selected, clusters = rank(
            pool, top_n=per_post, return_clusters=True, signatures=signatures
        )
        picks[kw] = (selected, clusters)
        for cluster in clusters:
            for a in cluster:
                taken_urls.add(canonical_url(a.url))
                taken_titles.add(a.title)
                taken_events.append(signatures.get(a.url, set()))

    return picks, off_topic


def _to_source(a: Article) -> SourceArticle:
    """선정 기사 → 초안 프롬프트용 SourceArticle."""
    return SourceArticle(
        title=a.title,
        url=a.url,
        date=a.date,
        source=a.press or a.source,
        subtopics=list(a.subtopics),
        matched_keyword=a.matched_keyword,
        content=a.body_clean[:BODY_LIMIT],
    )


def _build_bundles(
    keyword: str,
    selected: list[Article],
    clusters: list[list[Article]],
    run_at: str,
) -> tuple[list[dict], list[str]]:
    """한 키워드의 선정분을 4건씩 묶어 publish_bundles() 입력으로 만든다.

    반환: (묶음 목록, 보류된 기사 제목)
    묶음마다 member_urls 를 담는다. 초안 저장이 끝나면 이 URL 들이
    처리 이력에 들어간다 (같은 사건으로 합쳐진 기사 포함).
    """
    ready = [a for a in selected if a.subtopics]
    for a in selected:
        if not a.subtopics:
            log.warning(f"[{keyword}] 소주제 없음, 이번 묶기에서 제외: {a.title[:40]}")
    if len(ready) < BUNDLE_SIZE:
        log.info(f"[{keyword}] 소주제까지 만든 기사 {len(ready)}건 — {BUNDLE_SIZE}건이 안 돼 보류")
        return [], [a.title for a in ready]

    by_url = {a.url: a for a in ready}
    cluster_urls = {
        rep.url: [m.url for m in cluster]
        for rep, cluster in zip(selected, clusters)
    }

    groups = group_articles([_to_source(a) for a in ready])
    log.info(f"[{keyword}]\n" + format_groups(groups))

    bundles: list[dict] = []
    held: list[str] = []
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M")
    for g in groups:
        if g.kind == "held" or len(bundles) >= POSTS_PER_KEYWORD:
            held.extend(a.title for a in g.brief.articles)
            continue
        members = [by_url[a.url] for a in g.brief.articles]
        brief = g.brief
        bundles.append({
            "bundle_id": f"{stamp}-{keyword}-{len(bundles) + 1}",
            "page_id": "",                  # Content 는 초안 작성 후에 만든다
            "pages": [],                    # 뉴스 DB 를 쓰지 않는다
            "title": brief.title_hint,
            "url": next((a.url for a in brief.articles if a.url), ""),
            "subtopics": [t for a in brief.articles for t in a.subtopics],
            "body": brief.articles[0].content,
            "brief": brief,
            "kind": g.kind,
            "reason": f"[{keyword}] {g.reason}",
            "search_keyword": keyword,
            "search_ranks": [m.search_rank for m in members],
            "run_at": run_at,
            # 챕터 대표 기사. 저장이 끝나면 이 기사들의 시그니처를 사건
            # 이력에 남긴다 (합쳐진 기사까지 남기면 state.json 이 불어난다).
            "rep_urls": [m.url for m in members],
            "member_urls": [
                u for m in members for u in cluster_urls.get(m.url, [m.url])
            ],
        })
    return bundles, held


def run(
    *,
    dry_run: bool = False,
    skip_duplicates: bool = True,
    skip_subtopic: bool = False,
    no_notion: bool = False,
    with_images: bool = True,
    with_quality: bool = True,
) -> None:
    setup()  # 로깅 초기화 (콘솔 + 파일)
    log.info("=" * 50)
    log.info(
        "TaekwonW 자동 파이프라인 시작"
        + (" [DRY-RUN]" if dry_run else "")
        + (" [NO-SUBTOPIC]" if skip_subtopic and not dry_run else "")
        + (" [NO-NOTION]" if no_notion and not dry_run else "")
    )
    log.info(
        "레인 "
        + " → ".join(f"{l.name}({','.join(l.keywords)})" for l in LANES)
        + f" · 레인당 {POSTS_PER_KEYWORD}편"
    )
    # 실제로 적용된 임계값을 남긴다. .env 와 collect.yml 이 갈라져도
    # 로컬·Actions 로그의 이 줄만 비교하면 드러난다 (실측: EVENT_SIMILARITY
    # 를 .env 에서 고쳤는데 셸 환경변수가 이기고 있었다).
    log.info("선정 " + threshold_summary())
    log.info(
        f"수집 묶음 {BUNDLE_SIZE}건/편 · "
        + " · ".join(f"{l.name} {l.display}건/{l.lookback_days}일" for l in LANES)
    )
    log.info("=" * 50)

    # 수집회차. 같은 실행에서 나온 글이 같은 값을 가져야 보드에서
    # '수집회차 → 검색순위' 정렬이 실행 단위로 묶인다.
    run_at = datetime.now(KST).replace(second=0, microsecond=0).isoformat()

    try:
        # 0) 이전 실행 상태
        st = state.load()

        # 1~2) 레인별 수집 + 날짜 필터
        #      레인마다 수집 건수와 조회 일수가 다르므로 따로 돈다.
        #      레인 순서대로 이어 붙이면 뒤의 dedupe 가 앞 레인 기사를 남긴다
        #      (= 앞 레인이 우선). 설정에 적은 순서가 곧 우선순위다.
        articles: list[Article] = []
        for lane in LANES:
            got = naver_news_client.collect_all(lane.keywords, lane.display)
            kept = filter_since(got, st["last_collected_at"], lane.lookback_days)
            log.info(
                f"[{lane.name}] 수집 {len(got)}건 → 날짜 필터 {len(kept)}건 "
                f"(키워드당 {lane.display}건 · 최근 {lane.lookback_days}일)"
            )
            articles += kept

        # 3) 처리 이력 제외
        if skip_duplicates:
            processed = set(st["processed_urls"])
            before = len(articles)
            articles = [a for a in articles if _norm(a.url) not in processed]
            log.info(f"처리 이력 제외 {before - len(articles)}건 → {len(articles)}건")

        if not articles:
            log.info("새로운 기사가 없습니다. 파이프라인 종료")
            if not dry_run:
                notify_empty("자동 파이프라인", "새로운 기사가 없습니다")
            return

        collected_until = latest_published(articles)

        # 4) 키워드별로 나누고 본문 확보 (URL 당 한 번)
        lanes = _extract_once(_split_by_lane(articles))

        # 5) 레인별 선정 — 앞 레인이 고른 기사는 뒤 레인 후보에서 뺀다
        per_post = BUNDLE_SIZE * POSTS_PER_KEYWORD
        #    이전 회차 초안의 사건도 뺀다. 기간은 가장 긴 레인 조회일수와 같다 —
        #    그보다 오래된 사건의 기사는 날짜 필터가 이미 거른다.
        keep_days = max(l.lookback_days for l in LANES)
        known = state.recent_events(st, keep_days)
        if known:
            log.info(f"사건 이력 {len(known)}건 (최근 {keep_days}일 초안)")
        picks, off_topic = select_by_lane(lanes, per_post, known_events=known)

        save_csv([a for sel, _ in picks.values() for a in sel], prefix="selected")

        # 5-1) 4건 단위로 자른다 — 묶음이 안 되는 나머지에 소주제를 만들면
        #      LLM 호출만 쓰고 버린다. 잘린 기사는 다음 실행에서 다시 후보가 된다.
        held_titles: list[str] = []
        for kw, (sel, clus) in picks.items():
            usable = len(sel) // BUNDLE_SIZE * BUNDLE_SIZE
            if usable < len(sel):
                log.info(
                    f"[{kw}] 선정 {len(sel)}건 중 {len(sel) - usable}건은 "
                    f"{BUNDLE_SIZE}건 단위를 채우지 못해 다음 실행으로 넘깁니다"
                )
                held_titles += [a.title for a in sel[usable:]]
            picks[kw] = (sel[:usable], clus[:usable])

        all_selected = [a for sel, _ in picks.values() for a in sel]
        if not all_selected:
            log.info(f"{BUNDLE_SIZE}건을 채운 레인이 없습니다. 파이프라인 종료")
            if not dry_run:
                state.mark_processed(st, [_norm(u) for u in off_topic], collected_until)
                notify_empty(
                    "자동 파이프라인",
                    f"키워드별 {BUNDLE_SIZE}건을 채우지 못해 초안을 쓰지 않았습니다 "
                    f"(보류 {len(held_titles)}건)",
                )
            return

        # 6) 소주제 생성 (LLM #1) — 키워드 구분 없이 한 번에 배치로 보낸다.
        #    결과는 기사 객체에 붙으므로 키워드별 목록에 그대로 반영된다.
        if dry_run or skip_subtopic:
            reason = "dry-run" if dry_run else "--no-subtopic"
            log.info(f"소주제 생성 건너뜀 ({reason}) — 선정 결과만 보고 끝냅니다")
            for kw, (sel, _) in picks.items():
                log.info(f"[{kw}] 선정 {len(sel)}건 — 소주제가 없어 묶지 않습니다")
            return
        generate_all(all_selected)

        # 7) 레인별 묶기
        bundles: list[dict] = []
        for lane in LANES:
            kw = lane.name
            selected, clusters = picks.get(kw, ([], []))
            if not selected:
                continue
            got, held = _build_bundles(kw, selected, clusters, run_at)
            bundles += got
            held_titles += held
            log.info(f"[{kw}] 묶음 {len(got)}편 · 보류 {len(held)}건")

        if not bundles:
            log.info("완성된 묶음이 없습니다. 보류 기사는 다음 실행에서 다시 후보가 됩니다")
            if not no_notion:
                state.mark_processed(st, [_norm(u) for u in off_topic], collected_until)
                notify_empty(
                    "자동 파이프라인",
                    f"키워드별 {BUNDLE_SIZE}건을 채우지 못해 초안을 쓰지 않았습니다 "
                    f"(보류 {len(held_titles)}건)",
                )
            return

        # 8) 초안 작성 → Content 저장 → 삽화 → 로컬 저장
        saved = publish_bundles(
            bundles,
            dry_run=no_notion,
            with_images=with_images,
            with_quality=with_quality,
        )

        if no_notion:
            log.info("Notion 사용 안 함 (--no-notion) — 처리 이력을 남기지 않습니다")
            return

        # 9) 처리 이력 — 저장된 글의 기사(+같은 사건) · 주제 부적합 기사
        done = [u for it in saved for u in it.get("member_urls", [])]
        signatures = build_signatures(_pool(lanes))
        events = [
            signatures[u] for it in saved for u in it.get("rep_urls", [])
            if signatures.get(u)
        ]
        state.mark_processed(
            st,
            [_norm(u) for u in done + off_topic],
            collected_until,
            events=events,
            keep_days=keep_days,
        )
        failed = len(bundles) - len(saved)
        log.info(
            f"파이프라인 종료 — 초안 {len(saved)}/{len(bundles)}편 저장"
            + (f" (실패 {failed}편은 다음 실행에서 다시 후보)" if failed else "")
        )
        if not saved:
            notify_empty("자동 파이프라인", f"초안 {len(bundles)}편을 모두 저장하지 못했습니다")
    except Exception as e:
        log.exception("파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("자동 파이프라인", e)
        raise
"""파이프라인 오케스트레이션.

상태 로드 → 수집(naver + google) → 날짜 필터(이전 수집일 이후) → 중복 제거
→ 본문 추출 → 정제 → Notion 전체 저장(수집됨)
→ 선정(랭킹) → 소주제 생성(LLM) → 선정분 승격(선정됨)
→ 묵은 대기 카드 정리(3일 경과) → 보관 정리

전체 기사를 먼저 저장하는 이유:
  선정 로직이 놓친 기사를 사람이 눈으로 확인하고 직접 고를 수 있게 한다.
  직접 고를 때는 상태를 '선정요청'으로 바꾸고 main_subtopic.py 를 돌린다.
  쌓이는 것을 막기 위해 상태별 보관 기간이 지나면 자동으로 휴지통에 보낸다.

이후 단계는 별도 실행이다:
  [사람] 보드에서 카드를 슬롯에 배치하고 [초안 작성] 버튼을 누른다
  [폴링] '초안요청' 을 감지해 confirm(검증) → publish(초안 작성)

묵은 대기 카드 정리:
  confirm 이 규칙 위반으로 거부한 카드는 '선정됨' 으로 되돌아오고
  '대기' 칸으로 내려간다. 다음 content 실행에서 새 기사와 다시 묶이지만,
  짝이 끝내 안 채워지면 시의성을 잃은 채 후보 목록에 계속 남는다.
  사흘이 지나도록 묶이지 않은 것은 '보류' 로 넘긴다.

  거부 사유는 보드 밑 '승인 불가 현황' 콜아웃에 쌓인다. 그 콜아웃은
  날짜가 바뀌었을 때만 비운다(reset_notice) — 하루 안에 collect 를
  여러 번 돌려도 그날 사유가 날아가지 않는다.

LLM 호출 조건:
  8단계(소주제 생성)만 LLM을 쓴다. 나머지는 전부 결정론적 코드다.
  --dry-run 과 --no-subtopic 은 이 단계를 건너뛴다. 그 경우 소주제가 비므로
  승격 상태를 '선정됨'이 아니라 '선정요청'으로 두어, publish 가 빈 초안을
  쓰는 대신 subtopic 커맨드가 이어받게 한다.

에러 처리 정책:
  - 기사 단위 오류(디코딩/다운로드/추출/Notion 저장 실패)
    → 해당 기사만 건너뛰고 계속 진행 (WARNING 로그).
      Notion 실패 건은 state에 기록되지 않아 다음 실행에서 자동 재시도.
  - 파이프라인 중단급 오류(네이버 API 인증 실패, data source 조회 실패, 환경변수 누락)
    → ERROR 로그 후 즉시 중단. 계속해봐야 전부 실패하는 종류의 에러.
  - 모든 로그는 콘솔 + logs/pipeline_YYYYMMDD.log 파일에 동시 기록.
"""
import csv
from datetime import datetime

from core import state_store as state
from agents.collecting.body_cleaner import clean_all
from tools import google_rss_client, naver_news_client
from config.settings import PROCESSED_DIR, NOTION_BOARD_PAGE_ID   
from agents.collecting.date_filter import filter_since, latest_published
from tools.article_fetcher import extract_all
from core.logger import get_logger, setup
from core.article_models import Article, dedupe
from tools.slack_notifier import notify_empty, notify_failure, notify_selected
from agents.drafting.article_grouper import classify_publish_mode
from agents.ranking.ranking_agent import rank
from agents.subtopic.subtopic_agent import generate_all
from tools.notion_store import (
    STATUS_COLLECTED,
    STATUS_DEFAULT,
    STATUS_REQUESTED,
    cleanup_expired,
    cleanup_stale_wait,
    find_page_by_url,
    promote_to_selected,
    reset_notice,
    resolve_data_source_id,
    save_all,
)

log = get_logger(__name__)


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


def _cleanup(data_source_id: str) -> None:
    """묵은 대기 카드 정리 + 보관 정리.

    수집 결과와 무관하게 매 실행 돌아야 한다. 어느 쪽도 오늘 들어온
    기사를 보지 않는다 — 어제까지 쌓인 것의 뒤처리다.

    예전에는 이 두 호출이 run() 의 12단계에만 있었다. 그 위에 '새로운
    기사가 없습니다' 조기 반환이 있어, 주말처럼 신규 기사가 0건인 날에는
    정리가 통째로 건너뛰어졌다(2026-09-12 거부분이 이틀간 방치됐다).

    실패해도 파이프라인을 세우지 않는다. 다음 실행에서 다시 시도한다.
    """
    # 대기 카드를 먼저 치워야 그 결과가 같은 실행의 묶음 배정에 반영된다.
    try:
        cleanup_stale_wait(data_source_id)
    except Exception as e:
        log.warning(f"묵은 대기 카드 정리 실패(다음 실행에서 재시도): {e}")

    try:
        cleanup_expired(data_source_id)
    except Exception as e:
        log.warning(f"보관 정리 실패(다음 실행에서 재시도): {e}")


def run(
    *,
    dry_run: bool = False,
    skip_duplicates: bool = True,
    skip_subtopic: bool = False,
    no_notion: bool = False,
) -> None:
    setup()  # 로깅 초기화 (콘솔 + 파일)   
    log.info("=" * 50)
    log.info(
        "TaekwonW 뉴스 파이프라인 시작"
        + (" [DRY-RUN]" if dry_run else "")
        + (" [NO-NOTION]" if no_notion and not dry_run else "")
    )
    log.info("=" * 50)

    # 새 하루 시작 — 어제 누적된 '승인 불가 현황'을 비운다.
    # data_source_id 와 무관하게(뉴스DB가 아니라 고정 페이지의 블록이라)
    # 새 기사가 있든 없든 collect 가 도는 시점에 항상 실행한다.
    # 콜아웃이 이미 오늘 날짜면 reset_notice 가 알아서 건너뛰므로, 하루에
    # 여러 번 돌려도 그날 오전에 쌓인 사유가 날아가지 않는다.
    #
    # 실패해도 넘어간다. 이 호출은 try 블록 밖이라 여기서 예외가 나면
    # 수집이 시작도 못 하고, notify_failure 가 try 안에 있어 Slack 알림도
    # 가지 않는다. 콜아웃 비우기는 부수 작업이지 수집의 전제가 아니다.
    if NOTION_BOARD_PAGE_ID and not dry_run and not no_notion:
        try:
            reset_notice(NOTION_BOARD_PAGE_ID)
        except Exception as e:
            log.warning(f"'승인 불가 현황' 초기화 실패(이어서 진행): {e}")

    try:
        # 0) 이전 실행 상태 로드
        st = state.load()

        # 1) 수집 (두 소스 → 공통 Article 스키마)
        articles = naver_news_client.collect() + google_rss_client.collect()

        # 2) 날짜 필터 — 이전 수집일 이후 발행분만 (두 소스 공통 기준)
        articles = filter_since(articles, st["last_collected_at"])

        # 3) 중복 제거: 이번 실행 내 URL 중복 + 이전 실행에서 처리한 URL
        articles = dedupe(articles)
        processed = set(st["processed_urls"])
        articles = [a for a in articles if a.url.strip().rstrip("/") not in processed]
        log.info(f"중복 제거 후 {len(articles)}건 (신규 기사)")

        if not articles:
            log.info("새로운 기사가 없습니다.")
            # 수집이 비어도 정리는 돌린다. 대기 카드 정리와 보관 정리는
            # 어제까지 쌓인 것의 뒤처리라 오늘 신규 기사 수와 무관하다.
            if not dry_run and not no_notion:
                try:
                    _cleanup(resolve_data_source_id())
                except Exception as e:
                    log.warning(f"정리 단계를 건너뜁니다(Notion 접근 실패): {e}")
            log.info("파이프라인 종료")
            return

        # 이번 수집분의 최신 발행시각 → 다음 실행의 '이전 수집일' 기준
        # (지금 시각이 아니라 실제 기사 발행일을 쓴다: RSS 노출 지연 대비)
        collected_until = latest_published(articles)

        # 4) 본문 추출 (requests + 인코딩 보정 + trafilatura, 병렬)
        articles = extract_all(articles)
        collected_count = len(articles)

        # 4-1) 정제 '전' 백업 — 여기서 남겨야 제외된 기사도 보존된다.
        #      clean_all 뒤에 두면 걸러진 기사가 CSV에 없어, 왜 말랐는지
        #      조사할 원본이 사라진다(2026-08-14: 13건 중 12건 제외, 조사 불가).
        #      Notion 저장(6단계)보다 앞이기도 해서 Notion 장애도 함께 대비된다.
        save_csv(articles, prefix="collected")

        # 5) 정제
        articles = clean_all(articles)

        # 6) Notion 전체 저장 (상태=수집됨) — 사람이 훑어볼 목록
        #
        #    Notion 을 쓸 수 없어도 파이프라인을 세우지 않는다. 랭킹과 소주제 생성은
        #    메모리의 articles 로 돌아가므로, 저장이 안 되더라도 '수집이 잘 되는지'
        #    CSV 로 확인할 수 있어야 한다. 키를 넣기 전이나 API 장애 때가 그렇다.
        #    저장을 건너뛰면 9·11·12 단계(승격·상태 갱신·보관 정리)도 함께 건너뛴다.
        data_source_id = ""
        saved_urls: list[str] = []
        use_notion = not dry_run and not no_notion
        if use_notion:
            try:
                data_source_id = resolve_data_source_id()
                saved_urls = save_all(
                    articles,
                    status=STATUS_COLLECTED,
                    dry_run=dry_run,
                    skip_duplicates=skip_duplicates,
                )
            except Exception as e:
                use_notion = False
                log.warning(
                    f"Notion 저장 실패 — 저장 없이 계속합니다: {e}"
                )
                log.warning(
                    "결과는 data/processed/ 의 CSV 로 확인하세요. "
                    "의도한 것이라면 --no-notion 을 붙이면 이 경고가 사라집니다"
                )
        elif no_notion:
            log.info("Notion 사용 안 함 (--no-notion) — CSV 로만 결과를 남깁니다")

        # 7) 선정 — 당일 트렌드 키워드 기반 랭킹 (코드, LLM 없음)
        selected = rank(articles)

        # 8) 소주제 생성 (LLM #1) — 선정분만
        #    LLM을 부르지 않는 경우:
        #      dry_run       Notion에 쓰지 않으면서 할당량만 태우는 것을 막는다
        #      skip_subtopic 수집만 다시 돌리고 싶을 때 (--no-subtopic)
        #    두 경우 모두 소주제가 비므로 상태를 '선정됨'으로 올리면 안 된다.
        #    publish 가 소주제 없는 페이지를 집어가 빈 초안을 쓰기 때문이다.
        #    대신 '선정요청'으로 올려 subtopic 커맨드가 이어받게 한다.
        if dry_run or skip_subtopic:
            reason = "dry-run" if dry_run else "--no-subtopic"
            log.info(f"소주제 생성 건너뜀 ({reason}) — LLM을 호출하지 않습니다")
            promote_status = STATUS_REQUESTED
        else:
            selected = generate_all(selected)
            promote_status = STATUS_DEFAULT

        # 9) 선정분 승격: 이미 만든 페이지를 갱신한다 (중복 생성 방지)
        if selected and use_notion:
            promoted = 0
            for a in selected:
                try:
                    page_id = find_page_by_url(data_source_id, a.url)
                    if not page_id:
                        log.warning(f"페이지를 찾지 못해 승격 실패: {a.title[:40]}")
                        continue
                    # 본문 길이로 발행 방식을 미리 표시한다 (승인 판단 근거).
                    mode = classify_publish_mode(len(a.body_clean))
                    promote_to_selected(
                        page_id, a, status=promote_status, publish_mode=mode
                    )
                    a.page_id = page_id  # Slack 알림의 Notion 링크에 사용
                    promoted += 1
                    log.info(f"선정 승격: {a.score_norm:3d}점 | {a.title[:40]}")
                except Exception as e:
                    log.warning(f"승격 실패 ({a.title[:40]}): {e}")
            log.info(f"선정 승격 완료 {promoted}/{len(selected)}건 (상태={promote_status})")
            if promote_status == STATUS_REQUESTED:
                log.info("소주제는 `python run.py subtopic` 으로 생성하세요")

        # 10) CSV 백업 (선정분 — 소주제 포함)
        save_csv(selected, prefix="selected")

        # 10-1) Slack 알림 — 승인 게이트로 유도한다
        #       결과가 없어도 알림을 보낸다: 알림이 아예 없으면 cron 실패와 구분되지 않는다
        if not dry_run:
            if selected:
                notify_selected(selected, {"collected": collected_count})
            else:
                notify_empty("뉴스 수집", "선정 조건을 만족하는 기사가 없습니다")

        # 11) 상태 갱신 — Notion 저장 '성공' 건만 기록
        #     Notion 을 쓰지 않았다면 기록하지 않는다. 저장되지 않은 기사를
        #     '처리 완료'로 남기면, 나중에 Notion 을 붙였을 때 그 기사들이
        #     영영 저장되지 않는다.
        if use_notion:
            normalized = [u.strip().rstrip("/") for u in saved_urls]
            state.mark_processed(st, normalized, collected_until)
        elif not dry_run:
            log.info("Notion 저장을 하지 않아 처리 이력을 남기지 않습니다 (다음 실행에서 다시 수집)")

        # 12) 보관 정리 (묵은 대기 카드 → 보류 → 휴지통)
        if use_notion:
            _cleanup(data_source_id)

        log.info("파이프라인 종료")
    except Exception as e:
        # 중단급 오류: traceback 전체를 로그 파일에 남기고 비정상 종료 코드로 전파
        log.exception("파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("뉴스 수집 파이프라인", e)
        raise
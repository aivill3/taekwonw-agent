"""직접 선정 기사의 소주제 생성 (사람이 고른 기사 처리).

용도:
  수집 파이프라인은 상위 5건만 자동 선정한다. 나머지는 '수집됨' 상태로 남아
  사람이 눈으로 훑어볼 수 있다. 그중 쓸 만한 기사를 발견하면 Notion에서
  상태를 '선정요청'으로 바꾸고 이 스크립트를 돌린다.
  collect 를 --no-subtopic 으로 돌렸을 때 남는 선정분도 여기서 처리한다.

흐름:
  '선정요청' 조회 → 원문 본문 확보 → 채점·사건 묶기 → 소주제 생성(LLM #1)
  → 대표는 '선정됨'으로, 병합된 기사는 '수집됨'으로

채점·사건 묶기를 여기서도 하는 이유:
  둘 다 rank() 안에만 있어서, 이 경로로 들어온 기사는 점수가 0으로 남고
  같은 사건 기사가 전부 통과했다. (실측: 10건 중 한마당 5건, 무주 2건)
  rank() 를 그대로 부르지 않는 것은 그 안의 주제 필터·TOP_N 절단이
  사람의 선택을 무시하기 때문이다. score_and_cluster() 는 채점과 묶기만 한다.

이후는 자동 선정분과 동일하다. 승인 체크 → main_publish.py.
"""
from core.logger import get_logger, setup
from tools.slack_notifier import notify_failure, notify_selected
from core.article_models import Article
from tools.notion_store import (
    STATUS_COLLECTED,
    STATUS_REQUESTED,
    fetch_by_status,
    fetch_page_body,
    promote_to_selected,
    resolve_data_source_id,
    update_status,
)
from agents.drafting.article_grouper import classify_publish_mode
from agents.ranking.ranking_agent import score_and_cluster
from agents.subtopic.subtopic_agent import generate_all

log = get_logger(__name__)


def run(*, dry_run: bool = False, dedupe: bool = True) -> None:
    setup()
    log.info("=" * 50)
    log.info("직접 선정 기사 소주제 생성" + (" [DRY-RUN]" if dry_run else ""))
    log.info("=" * 50)

    try:
        data_source_id = resolve_data_source_id()

        items = fetch_by_status(data_source_id, STATUS_REQUESTED)
        if not items:
            log.info(
                f"'{STATUS_REQUESTED}' 항목이 없습니다. "
                f"Notion에서 원하는 기사의 상태를 '{STATUS_REQUESTED}'으로 바꿔주세요."
            )
            return
        log.info(f"직접 선정 항목 {len(items)}건")
        for it in items:
            log.info(f"  - {it['title'][:50]}")

        # 소주제 생성에는 원문 본문이 필요하다 (페이지 본문에 저장돼 있음)
        articles: list[Article] = []
        for it in items:
            body = fetch_page_body(it["page_id"])
            if not body:
                log.warning(f"원문 본문을 찾지 못함, 제외: {it['title'][:40]}")
                continue
            a = Article(
                title=it["title"],
                url=it["url"],
                source="manual",
                body_clean=body,
            )
            a.page_id = it["page_id"]  # 승격 시 사용
            articles.append(a)

        if not articles:
            log.warning("처리할 기사가 없습니다.")
            return

        # 채점 + 사건 묶기 (LLM 없음). 점수는 이 목록 안에서의 상대값이다.
        articles, merged = score_and_cluster(articles, dedupe=dedupe)

        # 소주제 생성 (자동 선정분과 동일한 로직·모델 체인)
        done = generate_all(articles)
        if not done:
            log.warning("소주제가 생성된 기사가 없습니다.")
            return

        if dry_run:
            for a in done:
                cnt = f" · {a.report_count}개 매체" if a.report_count > 1 else ""
                log.info(f"[dry-run] {a.score_norm:3d}점{cnt} | {a.title[:45]}")
                for i, s in enumerate(a.subtopics, 1):
                    log.info(f"    {i}. {s}")
            for a, rep in merged:
                log.info(f"[dry-run] 병합 → '{STATUS_COLLECTED}' 되돌림: {a.title[:40]}")
            log.info("[dry-run] Notion에 저장하지 않았습니다.")
            return

        # 대표 기사: 소주제·점수를 채우고 '선정됨'으로 승격
        promoted = []
        for a in done:
            try:
                mode = classify_publish_mode(len(a.body_clean))
                promote_to_selected(a.page_id, a, publish_mode=mode)
                promoted.append(a)
                log.info(f"선정 승격: {a.score_norm:3d}점 · {mode} | {a.title[:45]}")
            except Exception as e:
                log.warning(f"승격 실패 ({a.title[:40]}): {e}")

        # 병합된 기사: '수집됨'으로 되돌린다.
        # '선정요청'으로 두면 다음 실행에서 또 후보로 올라와 반복된다.
        for a, rep in merged:
            try:
                update_status(a.page_id, STATUS_COLLECTED)
                log.info(f"병합되어 '{STATUS_COLLECTED}'으로 되돌림: {a.title[:40]}")
            except Exception as e:
                log.warning(f"상태 되돌리기 실패 ({a.title[:40]}): {e}")

        log.info(f"승격 완료 {len(promoted)}/{len(done)}건 (병합 {len(merged)}건)")
        if promoted:
            notify_selected(promoted)
        log.info("파이프라인 종료")
    except Exception as e:
        log.exception("직접 선정 파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("직접 선정 소주제 생성", e)
        raise
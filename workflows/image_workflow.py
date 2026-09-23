"""이미 발행된 초안에 삽화를 나중에 붙인다.

왜 별도 명령인가:
  CPU에서 로컬 이미지 생성은 장당 수십 초~수 분이다. collect 안에서 돌리면
  초안 2편에 삽화 4장씩이면 30분 넘게 콘솔이 묶인다. 초안 자체는 몇 분이면
  끝나므로, 글을 먼저 저장해 확인하고 삽화는 시간 될 때 따로 채우는 편이 낫다.
  collect 안에서 삽화가 실패한 글을 채울 때도 쓴다.

    py run.py collect --no-images     초안까지만 (빠름)
    py run.py images                  삽화 생성 후 Notion에 삽입 (느림)

  GitHub Actions 는 스톡 사진으로 채우므로 collect 한 번으로 끝난다.

흐름:
  Content DB '작성완료' 조회 → 본문에서 초안 읽기 → 소제목 추출
  → 삽화 생성 → 숫자 자리 뒤에 이미지 삽입

뉴스 DB 가 아니라 Content DB 를 본다
---------------------------------
초안은 Content 페이지에 들어간다. 예전에는 뉴스 페이지에 썼기 때문에
여기서도 뉴스 DB 를 뒤졌는데, 그러면 지금은 한 건도 찾지 못한다.

  실측(2026-09-02): '작성완료' 뉴스 13건이 전부 '초안 없음(묶음 종속)'
  으로 걸러져 대상 0건이 됐다.

Content 한 행이 곧 글 한 편이라 '묶음 종속' 판정도 필요 없어졌다.
4건짜리 묶음이 4번 후보로 올라오던 것이 1번으로 준다.

이미 삽화가 붙은 페이지는 건너뛴다. 두 번 돌려도 중복되지 않는다.

기본은 '오늘 작성된 초안'만 대상으로 한다(IMAGE_MAX_AGE_DAYS).
'작성완료' 전체를 쓸면 과거 백로그까지 다시 그리게 되는데, CPU 생성은
장당 수십 초라 15건이면 몇 시간이 걸리고 지난 글에 지금 삽화를 붙일 실익도 적다.

    py run.py images                  오늘 작성된 초안만 (기본)
    py run.py images --days 7         최근 7일
    py run.py images --days 0         전체 — 백로그를 의도적으로 채울 때만

이미 붙은 삽화가 마음에 들지 않으면 --redo 로 다시 만든다. 기존 이미지
블록을 지우고 새로 생성한다. 원문 기사 영역의 사진은 건드리지 않는다.

    py run.py images --redo --limit 1   한 편만 다시 만들어 결과를 본다
"""
from datetime import datetime

from config.draft_config import ILLUST_MODEL
from config.settings import KST
from core.logger import get_logger, setup
from tools.notion_store import (
    delete_draft_images,
    fetch_page_body,
    insert_images_at_slots,
    list_blocks,
)
from tools.notion_content_store import (
    STATUS_WRITTEN,
    fetch_contents_by_status,
    resolve_content_data_source_id,
)
from tools.slack_notifier import notify_failure
from workflows.publish_workflow import collect_images, make_slug, section_headings

log = get_logger(__name__)

DRAFT_HEADING = "블로그 초안"

# 삽화를 붙일 초안의 최대 나이(일). 기본 1 = 오늘 작성된 초안만.
#
# 이 값이 없으면 '작성완료' 전체가 대상이 된다. 과거에 --no-images 로 발행했거나
# 429로 삽화를 못 받은 글이 백로그로 쌓여 있어서, 한 번 돌리면 수십 장을 만드느라
# CPU가 몇 시간 묶인다(실측: 15건 × 소제목 3개 = 45장).
# 지난 글은 이미 사람이 읽고 넘어간 뒤라 지금 삽화를 붙일 실익도 적다.
#
# 백로그를 의도적으로 채우려면 --days 0 (제한 없음).
IMAGE_MAX_AGE_DAYS = 1


def _age_days(iso: str) -> int | None:
    """작성 시각으로부터 지난 일수. 파싱할 수 없으면 None.

    publish_workflow._days_since 는 실패 시 0(=오늘)을 돌려주지만,
    여기서는 '모름'과 '오늘'을 구분해야 한다. 날짜를 못 읽는 것을 오늘로 처리하면
    필터가 조용히 무력화돼 백로그 전체를 다시 그리게 된다.
    """
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0, (datetime.now(KST) - dt.astimezone(KST)).days)


def _draft_of(page_id: str) -> tuple[str, bool]:
    """페이지 본문에서 초안 부분만 읽는다. (초안 텍스트, 이미지 이미 있음)

    '블로그 초안' 헤딩 아래가 초안이다. 그 위는 원문 기사라 제외한다.
    (fetch_page_body 는 반대로 헤딩 위만 읽는다)
    """
    lines: list[str] = []
    started = False
    has_image = False

    for b in list_blocks(page_id):
        btype = b.get("type", "")
        if btype.startswith("heading"):
            text = "".join(
                r.get("plain_text", "") for r in b.get(btype, {}).get("rich_text", [])
            )
            if DRAFT_HEADING in text:
                started = True
            continue
        if not started:
            continue
        if btype == "image":
            has_image = True
            continue
        if btype == "paragraph":
            lines.append(
                "".join(
                    r.get("plain_text", "")
                    for r in b.get("paragraph", {}).get("rich_text", [])
                )
            )

    return "\n\n".join(lines), has_image


def run(
    *,
    dry_run: bool = False,
    limit: int = 0,
    days: int = IMAGE_MAX_AGE_DAYS,
    redo: bool = False,
) -> None:
    setup()
    log.info("=" * 50)
    log.info(
        "발행된 초안에 삽화 " + ("다시 붙이기" if redo else "추가")
        + (" [DRY-RUN]" if dry_run else "")
    )
    log.info("=" * 50)

    try:
        content_ds = resolve_content_data_source_id()
        items = fetch_contents_by_status(content_ds, STATUS_WRITTEN)
        if not items:
            log.info(f"'{STATUS_WRITTEN}' Content 가 없습니다.")
            return

        targets: list[tuple[dict, str, list[str]]] = []
        skipped_done = skipped_empty = skipped_nomarker = 0
        skipped_old = 0
        undated = 0
        for it in items:
            # 나이 판정을 _draft_of 보다 먼저 한다. _draft_of 는 페이지마다
            # list_blocks(API 호출)를 하므로, 어차피 거를 페이지를 읽을 이유가 없다.
            if days > 0:
                age = _age_days(it.get("edited", "") or it.get("created", ""))
                if age is None:
                    undated += 1
                elif age > days:
                    skipped_old += 1
                    continue

            draft, has_image = _draft_of(it["page_id"])
            if has_image and not redo:
                skipped_done += 1
                log.info(f"이미 삽화가 있어 건너뜀: {it['title'][:40]}")
                continue
            if not draft.strip():
                # Content 한 행은 글 한 편이므로 초안이 없으면 이상한 상태다.
                # publish 가 Content 를 만든 뒤 본문 붙이기에서 끊겼을 수 있다.
                skipped_empty += 1
                log.warning(
                    f"초안 본문이 비어 건너뜀: {it['title'][:40]} "
                    f"— publish 가 중간에 끊겼는지 확인하세요"
                )
                continue
            headings = section_headings(draft)
            if not headings:
                # 초안은 있는데 `/* 소제목 */` 마커가 없다 — 이쪽이 진짜 확인 대상.
                # LLM이 형식을 안 지켰거나 초안 저장 형식이 바뀐 경우다.
                skipped_nomarker += 1
                log.warning(
                    f"초안에 소제목 마커가 없음, 건너뜀: {it['title'][:40]} "
                    f"({len(draft)}자)"
                )
                continue
            targets.append((it, draft, headings))

        scope = f"최근 {days}일" if days > 0 else "전체(제한 없음)"
        log.info(
            f"Content '{STATUS_WRITTEN}' {len(items)}건 · 범위 {scope} "
            f"→ 대상 {len(targets)}건 "
            f"(기간 밖 {skipped_old} · 삽화 완료 {skipped_done} · "
            f"본문 없음 {skipped_empty} · 마커 없음 {skipped_nomarker})"
        )
        if undated:
            # 날짜를 못 읽으면 거르지 않고 통과시켰다는 뜻이다. 조용히 넘기면
            # 필터가 duty를 못 하는데도 정상으로 보인다.
            log.warning(
                f"작성 시각을 읽지 못한 페이지 {undated}건은 기간 필터 없이 포함했습니다"
            )

        if not targets:
            log.info("삽화를 붙일 페이지가 없습니다.")
            return

        if limit:
            targets = targets[:limit]
            log.info(f"--limit {limit} 적용 → {len(targets)}건만 처리합니다")

        total = 0
        for it, draft, headings in targets:
            log.info(f"── {it['title'][:45]} (소제목 {len(headings)}개)")
            if dry_run:
                for i, h in enumerate(headings, 1):
                    log.info(f"   {i}. {h}")
                continue

            slug = make_slug(draft.split("\n", 1)[0].strip())

            # 다시 붙이기: 기존 삽화를 먼저 걷어낸다. 생성 전에 지우는 이유는
            # 생성이 실패해도 잘못된 삽화는 사라진 편이 낫기 때문이다 —
            # 잘못된 그림이 붙어 있는 것보다 빈 자리가 사람 눈에 띈다.
            if redo:
                delete_draft_images(it["page_id"])

            # 장면 계획에 넘길 본문. Content 페이지는 '블로그 초안' 헤딩 위에
            # 원문 기사를 토글로 담고 있으므로 그것을 읽는다. publish 경로와
            # 같은 재료라 삽화 품질이 갈리지 않는다.
            # 못 읽으면 초안 본문으로 대신한다 — 얕지만 소제목만 보는 것보다 낫다.
            source_body = fetch_page_body(it["page_id"]) or draft
            if source_body is draft:
                log.warning("   원문을 읽지 못해 초안 본문으로 장면을 계획합니다")

            # 모델명은 설정에서 가져온다. publish 경로에서는 초안을 쓴 모델이
            # 넘어오지만, 여기서는 Notion 에서 초안을 읽어오므로 그 정보가 없다.
            # 빈 문자열을 넘기면 분석·계획 LLM 호출이 실패하고, 모든 소제목이
            # 같은 폴백 장면으로 몰린다.
            images = collect_images(
                headings, slug, ILLUST_MODEL,
                title=it.get("title", ""), body=source_body,
            )
            if not images:
                log.warning("   삽화를 만들지 못했습니다.")
                continue
            inserted = insert_images_at_slots(it["page_id"], images)
            total += inserted
            log.info(f"   삽화 {inserted}장 삽입")

        if dry_run:
            log.info("[dry-run] 이미지를 만들지도 저장하지도 않았습니다.")
        else:
            log.info(f"완료: 총 {total}장 삽입")
    except Exception as e:
        log.exception("삽화 추가 중 중단급 오류 발생")
        if not dry_run:
            notify_failure("삽화 추가", e)
        raise
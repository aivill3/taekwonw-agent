"""묶음 배정 파이프라인 (승인 게이트 '앞' 단계).

흐름:
  '선정됨' 조회 → 원문 본문 확보 → 주제 적합성 재검사 → 같은 사건 중복 제거
  → group_articles() 로 묶기 → 각 기사에 '묶음' 슬롯 배정

여기서는 Content 를 만들지 않는다
--------------------------------
사람이 보드에서 카드를 끌어 재배치할 여지를 남기기 위해서다. 자동 묶기가
늘 맞는 것은 아니라, 확정 전에 손볼 수 있어야 한다.

    News DB + '묶음' select
      보드 뷰 (그룹: 묶음, 필터: 상태=선정됨)
        ┌─단독1─┐ ┌─묶음1──┐ ┌─대기──┐ ┌─제외──┐
        │ 기사A │ │ 기사B  │ │ 기사F │ │ 기사G │
        └───────┘ │ 기사C  │ └───────┘ └───────┘
                  │ 기사D  │  짝 부족    안 씀
                  │ 기사E  │  (선정됨)   (보류)
                  └────────┘
                     ↓ confirm
                 Content DB 생성 → publish

Content 행은 confirm_workflow 가 만든다. 기사수·목표분량·선정이유 같은
묶음 단위 값은 드래그로 바뀌므로, 확정 시점에 새로 계산해야 정확하다.

주제 적합성을 여기서도 보는 이유
------------------------------
is_on_topic() 은 rank() 안에서만, 즉 collect 시점에만 돈다. 그래서 이미
'선정됨'으로 저장된 기사에는 나중에 강화한 기준이 닿지 않는다.

  실측(2026-08-28): 필터를 고친 뒤에도 '선정됨' 43건 중 육상·e스포츠·
  지자체 종합 기사가 그대로 통과했다. 예전 기준으로 선정된 것들이기 때문이다.

순서도 중요하다. 주제 필터를 중복 제거보다 먼저 돌려야 한다. 뒤로 미루면
6,000자짜리 종합 기사가 같은 사건으로 묶여 대표 자리를 차지한다.
(_pick_representative 는 본문이 긴 쪽을 고른다)

  실측: '[패트롤] 경산시-칠곡군…' 이 '독일 ITF 틀 투어' 정상 기사 2건을
  흡수해 버렸다. 종합 기사에 그 내용이 한 단락 들어 있었기 때문이다.

중복 제거를 여기서도 하는 이유
-----------------------------
collect 의 dedupe 와 rank 의 사건 묶기는 '그날 배치 안에서만' 돈다.
승인이 밀려 여러 날치가 '선정됨'으로 쌓이면 날짜를 넘나드는 중복은
아무도 잡지 못한다.

  실측(2026-08-28, 43건): 나사렛대 필리핀 교류 2건, 춘천 세계품새선수권 2건,
  경상북도지사기 영주 2건, 거창군협회장배 2건, 대통령기 구례 2건,
  경찰청장기 영천 2건 — 같은 사건이 서로 다른 묶음에 들어가 있었다.

그대로 두면 같은 내용의 블로그 글이 두 편 나온다. 그래서 묶기 직전에
한 번 더 사건 단위로 합치고, 대표 1건만 남긴다.

대기 처리
--------
group_articles() 가 'held' 로 돌려준 묶음은 '대기' 슬롯에 넣는다. 4건을
못 채웠을 뿐이므로 상태는 '선정됨' 그대로 두어, 다음 실행에서 새로 들어온
기사와 다시 묶일 수 있게 한다.

'제외' 칸과 구분한 이유
---------------------
예전에는 '보류' 한 칸에 둘을 같이 넣었다. 그래서 confirm 이 "짝이 부족한
것"과 "사람이 안 쓰기로 한 것"을 구분하지 못하고 전부 '보류' 상태로
넘겨 버렸다. 다음 회차에 다시 묶여야 할 기사가 후보에서 사라진 것이다.

  대기 — 코드가 넣는다 (여기). confirm 이 상태를 건드리지 않는다.
  제외 — 사람이 끌어다 넣는다. confirm 이 '보류' 상태로 넘긴다.
"""
from core.logger import get_logger, setup
from core.article_models import Article
from tools.slack_notifier import notify_failure, notify_held
from tools.notion_store import (
    BUNDLE_GROUP_SLOTS,
    BUNDLE_SOLO_SLOTS,
    BUNDLE_WAIT,
    STATUS_COLLECTED,
    STATUS_DEFAULT,
    STATUS_HOLD,
    STATUS_REQUESTED_DRAFT,
    ensure_schema,
    fetch_by_status,
    fetch_page_body,
    resolve_data_source_id,
    set_bundle,
    update_status,
)
from agents.drafting.article_grouper import format_groups, group_articles
from agents.drafting.schema_draft_agent import to_source
from agents.ranking.ranking_agent import is_on_topic, score_and_cluster

log = get_logger(__name__)


def _days_since(iso: str) -> int:
    """ISO 시각으로부터 지난 일수. 파싱 실패 시 0."""
    from datetime import datetime

    from config.settings import KST

    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    return max(0, (datetime.now(KST) - dt.astimezone(KST)).days)


def _to_article(item: dict) -> Article:
    """Notion 항목을 사건 묶기용 Article 로 바꾼다.

    score_and_cluster() 는 title / body_clean / url 만 본다.
    press·source 는 로그에 '함께 보도한 매체'를 찍을 때 쓰인다.
    """
    a = Article(
        title=item["title"],
        url=item.get("url", ""),
        source=item.get("source", "notion"),
        press=item.get("press", ""),
    )
    a.body_clean = item.get("body", "")
    a.page_id = item["page_id"]
    return a


def _filter_on_topic(items: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """태권도가 주제가 아닌 기사를 걸러낸다. (통과 목록, [(제외된 항목, 사유), ...])"""
    passed: list[dict] = []
    dropped: list[tuple[dict, str]] = []

    for it in items:
        ok, reason = is_on_topic(_to_article(it))
        if ok:
            passed.append(it)
        else:
            dropped.append((it, reason))

    return passed, dropped


def _dedupe_events(items: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """같은 사건 기사를 합치고 (대표 목록, [(중복, 대표), ...]) 를 돌려준다.

    대표는 '본문이 가장 충실한' 기사다 (_pick_representative).
    같은 사건이라도 사진기사와 심층기사가 섞여 들어오기 때문이다.
    """
    by_page = {it["page_id"]: it for it in items}
    articles = [_to_article(it) for it in items]

    reps, merged_pairs = score_and_cluster(articles, dedupe=True)

    rep_items = [by_page[a.page_id] for a in reps if a.page_id in by_page]
    merged = [
        (by_page[dup.page_id], by_page[rep.page_id])
        for dup, rep in merged_pairs
        if dup.page_id in by_page and rep.page_id in by_page
    ]
    return rep_items, merged


def run(
    *,
    dry_run: bool = False,
    dedupe: bool = True,
    topic_filter: bool = True,
) -> None:
    setup()
    log.info("=" * 50)
    log.info("묶음 배정 시작" + (" [DRY-RUN]" if dry_run else ""))
    log.info("=" * 50)

    try:
        news_ds = resolve_data_source_id()

        # 1) 소주제까지 생성된 기사 조회
        items = fetch_by_status(news_ds, STATUS_DEFAULT)
        if not items:
            log.info(f"'{STATUS_DEFAULT}' 기사가 없습니다. 먼저 collect 를 실행하세요.")
            return
        log.info(f"'{STATUS_DEFAULT}' 기사 {len(items)}건")

        # 2) 원문 본문 확보 — 중복 판정·묶기·분량 계산에 모두 필요하다
        for it in items:
            it["body"] = fetch_page_body(it["page_id"])
            if not it["body"]:
                log.warning(f"원문 본문을 찾지 못함: {it['title'][:40]}")

        valid = [it for it in items if it["subtopics"] and it["body"]]
        for it in items:
            if not it["subtopics"]:
                log.warning(f"소주제 없음, 제외: {it['title'][:40]}")
            elif not it["body"]:
                log.warning(f"본문 없음, 제외: {it['title'][:40]}")

        if not valid:
            log.warning("묶을 수 있는 기사가 없습니다.")
            return

        # 3) 주제 적합성 재검사 — 중복 제거보다 '먼저' 해야 한다.
        #    뒤로 미루면 종합 기사가 정상 기사를 흡수한다.
        dropped: list[tuple[dict, str]] = []
        if topic_filter:
            before = len(valid)
            valid, dropped = _filter_on_topic(valid)
            if dropped:
                log.info(f"주제 부적합 {len(dropped)}건 제외 ({before}건 → {len(valid)}건)")
                for it, reason in dropped:
                    log.info(f"  제외: {it['title'][:44]}")
                    log.info(f"    └ {reason}")
            else:
                log.info("주제 부적합 기사 없음")

            if not valid:
                log.warning("주제 조건을 만족하는 기사가 없습니다.")
                return

        # 4) 같은 사건 중복 제거 — 묶기 '전에' 해야 한다.
        #    묶은 뒤에 하면 같은 사건이 이미 서로 다른 묶음에 흩어져 있다.
        merged: list[tuple[dict, dict]] = []
        held: list[dict] = []
        if dedupe:
            before = len(valid)
            valid, merged = _dedupe_events(valid)
            if merged:
                log.info(f"같은 사건 {len(merged)}건 합침 ({before}건 → {len(valid)}건)")
                for dup, rep in merged:
                    log.info(f"  중복: {dup['title'][:40]}")
                    log.info(f"    └ 대표: {rep['title'][:40]}")
            else:
                log.info("같은 사건 중복 없음")

        # 5) 묶기 — publish 에서 하던 것을 그대로 옮겨 왔다
        by_url = {it.get("url", ""): it for it in valid}
        groups = group_articles([to_source(it) for it in valid])
        log.info("\n" + format_groups(groups))

        if dry_run:
            log.info("[dry-run] 묶음을 배정하지 않았습니다.")
            if merged or dropped:
                log.info(
                    f"[dry-run] 주제 부적합 {len(dropped)}건 · 중복 {len(merged)}건도 "
                    f"상태를 바꾸지 않았습니다."
                )
            return

        # 6) 묶음 슬롯 배정
        #    Content 를 만들지 않는다. 사람이 보드에서 고칠 여지를 남기고,
        #    확정(confirm) 시점에 실제 배치대로 Content 를 만든다.
        ensure_schema(news_ds)  # '묶음' 속성이 없으면 여기서 생긴다

        # 초안 작성이 요청된 기사가 쓰고 있는 칸은 건너뛴다.
        # 버튼이 눌린 뒤 폴링이 처리하기까지 최대 30분이 걸리는데,
        # 그 사이 슬롯은 그대로다. 슬롯은 publish 가 초안을 저장한 뒤에
        # 반납한다(finish_article).
        #
        # 이 검사가 없으면 그 칸에 새 기사가 얹혀 한 칸에 두 묶음이 겹친다.
        # (실측 2026-09-02: 단독1~3 에 각각 2건씩 포개져 보였다)
        occupied = {
            it["bundle"]
            for it in fetch_by_status(news_ds, STATUS_REQUESTED_DRAFT)
            if it.get("bundle")
        }
        if occupied:
            log.info(
                f"초안 작성 중인 칸 {len(occupied)}개는 건너뜁니다: "
                f"{', '.join(sorted(occupied))}"
            )

        solo_slots = [s for s in BUNDLE_SOLO_SLOTS if s not in occupied]
        group_slots = [s for s in BUNDLE_GROUP_SLOTS if s not in occupied]
        assigned = 0
        overflow: list[str] = []

        for g in groups:
            members = [by_url.get(a.url) for a in g.brief.articles]
            members = [m for m in members if m]
            if not members:
                continue

            # 대기: '대기' 칸에 모아 둔다. 상태는 '선정됨' 그대로라
            # 다음 실행에서 새 기사와 다시 묶일 수 있다.
            # 사람이 안 쓰기로 하면 보드에서 '제외' 칸으로 끌어 옮긴다.
            if g.kind == "held":
                for m in members:
                    set_bundle(m["page_id"], BUNDLE_WAIT)
                    held.append({
                        "page_id": m["page_id"],
                        "title": m["title"],
                        "chars": len(m.get("body", "")),
                        "days": _days_since(m.get("created", "")),
                    })
                    log.info(f"대기: {m['title'][:40]} ({g.reason})")
                continue

            pool = solo_slots if len(members) == 1 else group_slots
            if not pool:
                # 슬롯이 모자라면 배정하지 않고 남긴다. 다음 실행에서
                # 앞선 것들이 '작성완료'로 빠진 뒤 자리가 난다.
                overflow.append(g.brief.title_hint[:40])
                continue

            slot = pool.pop(0)
            for m in members:
                try:
                    set_bundle(m["page_id"], slot)
                except Exception as e:
                    log.warning(f"묶음 배정 실패 ({m['title'][:30]}): {e}")

            assigned += 1
            log.info(f"[{slot}] {len(members)}건 · {g.brief.source_chars:,}자 — {g.reason}")
            for m in members:
                log.info(f"    - {m['title'][:52]}")

        if overflow:
            log.warning(
                f"슬롯 부족으로 {len(overflow)}편 미배정 "
                f"(단독 {len(BUNDLE_SOLO_SLOTS)}칸 · 묶음 {len(BUNDLE_GROUP_SLOTS)}칸 중 "
                f"{len(occupied)}칸이 작성 대기 중)"
            )
            for t in overflow:
                log.warning(f"    - {t}")

        # 7) 중복으로 합쳐진 기사는 '수집됨'으로 되돌린다.
        #    '선정됨'으로 두면 다음 실행에서 또 후보로 올라와 같은 판정을 반복한다.
        #    삭제하지 않는 이유: 대표 기사의 본문 추출이 실패했을 때
        #    사람이 이 기사를 다시 고를 수 있어야 한다.
        for dup, rep in merged:
            try:
                update_status(dup["page_id"], STATUS_COLLECTED)
            except Exception as e:
                log.warning(f"중복 상태 되돌리기 실패 ({dup['title'][:30]}): {e}")

        # 8) 주제 부적합 기사는 '보류'로 넘긴다.
        #    '선정됨'으로 두면 실행할 때마다 같은 판정을 반복한다.
        #    '수집됨'으로 되돌리면 훑어보기 목록에 다시 섞여 노이즈가 된다.
        #    판정이 틀렸다고 보이면 Notion 에서 '선정됨'으로 되돌리면 된다.
        for it, reason in dropped:
            try:
                update_status(it["page_id"], STATUS_HOLD)
            except Exception as e:
                log.warning(f"제외 상태 변경 실패 ({it['title'][:30]}): {e}")

        if held:
            notify_held(held)

        log.info(
            f"묶음 {assigned}편 배정 "
            f"(주제 부적합 {len(dropped)}건 · 중복 {len(merged)}건 · 대기 {len(held)}건)"
        )
        if assigned:
            log.info("Notion 보드에서 배치를 확인·조정한 뒤 `run.py confirm` 을 실행하세요.")
        log.info("파이프라인 종료")

    except Exception as e:
        log.exception("묶음 배정 파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("묶음 배정", e)
        raise
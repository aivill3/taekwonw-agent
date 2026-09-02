"""묶음 확정 파이프라인 (보드 배치를 굳힌다).

흐름:
  '선정됨' 조회 → '묶음' 슬롯별로 모음 → 규칙 검사
  → 통과한 슬롯에 묶음ID 부여 → 슬롯을 비우고 상태를 '초안대기'로

Content 를 만들지 않는다
----------------------
예전에는 여기서 Content 행을 만들었다. 그런데 그 시점에는 블로그 제목도
본문도 없어서 '⏳ …' 임시 제목을 달았다가 publish 가 덮어써야 했고,
확정을 취소하려면 뉴스 DB 와 Content DB 두 곳을 손봐야 했다.

이제 Content 는 publish 가 초안을 완성한 뒤에 만든다. Content DB 는
'쓸 예정인 글의 대기열'이 아니라 '써진 글의 보관함'이 된다.

  확정 취소 = 보드에서 카드를 '선정됨'으로 되돌리면 끝

묶음ID 를 붙이는 이유
-------------------
'단독1'~'묶음4' 슬롯은 열 칸뿐이라 회차마다 돌려쓴다. 슬롯만으로는
"이 카드가 어느 글에 속하는지"를 회차를 넘어 말할 수 없다.

그래서 확정 시점에 '20260902-01' 같은 고유 이름을 붙인다. publish 는
슬롯이 아니라 이 이름으로 묶음을 되살린다.

슬롯은 여기서 비우지 않는다
-------------------------
확정했다고 카드가 보드에서 사라지면 무엇이 초안을 기다리는 중인지
볼 수 없다. 슬롯은 초안이 나간 뒤 publish 가 반납한다(finish_article).

  확정 전   묶음=단독1   묶음ID=(빈값)       상태=선정됨
  확정 후   묶음=단독1   묶음ID=20260902-01  상태=초안대기   ← 보드에 남음
  발행 후   묶음=(빈값)  묶음ID=20260902-01  상태=작성완료   ← 칸이 빈다

대신 그 사이에는 칸이 점유된 상태다. content 가 그 칸을 건너뛰도록
되어 있어(occupied 검사) 새 기사가 얹히지 않는다.

왜 검사가 여기 있나
------------------
Notion 보드는 "이 칸에 4개까지만"을 막지 못한다. 다섯 번째 카드를 끌어
놓아도 UI 가 거부하지 않는다. 그래서 확정 시점에 코드가 검사한다.

    단독N   기사 1건 + 소주제 4개
    묶음N   기사 정확히 4건
    대기    건드리지 않는다 ('선정됨' 유지)
    제외    상태만 '보류'로
    미배정  건드리지 않는다

규칙을 어긴 슬롯은 통째로 건너뛴다. 상태를 바꾸지 않으므로 보드에서
고친 뒤 다시 확정하면 된다.

대기와 제외를 나눈 이유
---------------------
예전에는 '보류' 한 칸이었고 confirm 이 그 칸의 카드를 전부 '보류'
상태로 넘겼다. 그런데 그 칸에는 성격이 다른 둘이 섞여 있었다.

  content 가 넣은 것 — 4건을 못 채웠을 뿐. 다음 회차에 다시 묶여야 한다.
  사람이 옮긴 것     — 발행하지 않기로 한 것.

앞의 것까지 '보류'로 넘어가면서, 짝만 기다리면 될 기사가 후보에서
사라졌다. 그래서 칸을 '대기'(코드)와 '제외'(사람)로 나눴다.

판정 문구
--------
--check 를 붙이면 아무것도 확정하지 않고 '묶음상태' 속성에만 결과를 쓴다.
보드 카드 아래에 '✅ 승인 가능' / '⚠️ 승인 불가' 가 칩으로 보인다.

Notion 은 조건부 문구를 실시간으로 띄우지 못하므로, 카드를 옮긴 뒤에는
--check 를 다시 돌려야 갱신된다. 칸 제목 옆의 '건수'는 Notion 이
실시간으로 보여주니 함께 보면 된다.
"""
from datetime import datetime

from config.settings import KST
from core.logger import get_logger, setup
from tools.slack_notifier import notify_failure
from tools.notion_store import (
    BUNDLE_EXCLUDE,
    BUNDLE_GROUP_SLOTS,
    BUNDLE_LEGACY_HOLD,
    BUNDLE_SOLO_SLOTS,
    BUNDLE_STATE_EXCLUDE,
    BUNDLE_STATE_NG,
    BUNDLE_STATE_NONE,
    BUNDLE_STATE_OK,
    BUNDLE_STATE_WAIT,
    BUNDLE_WAIT,
    STATUS_DEFAULT,
    STATUS_HOLD,
    STATUS_LINKED,
    confirm_bundle,
    ensure_schema,
    fetch_by_status,
    resolve_data_source_id,
    set_bundle_id,
    set_bundle_state,
    update_status,
)
from agents.drafting.article_grouper import CHAPTERS_PER_POST, BUNDLE_SIZE

log = get_logger(__name__)


def _validate(slot: str, members: list[dict]) -> tuple[bool, str]:
    """슬롯 하나의 배치가 규칙에 맞는지 본다. (통과여부, 사유)"""
    n = len(members)

    if slot in BUNDLE_SOLO_SLOTS:
        if n != 1:
            return False, f"단독 슬롯에 {n}건 (1건이어야 함)"
        topics = len(members[0]["subtopics"])
        if topics < CHAPTERS_PER_POST:
            return False, f"소주제 {topics}개 (단독은 {CHAPTERS_PER_POST}개 필요)"
        return True, f"단독 1건 · 소주제 {topics}개"

    if slot in BUNDLE_GROUP_SLOTS:
        if n != BUNDLE_SIZE:
            short = BUNDLE_SIZE - n
            detail = f"{abs(short)}건 {'부족' if short > 0 else '초과'}"
            return False, f"묶음 슬롯에 {n}건 ({BUNDLE_SIZE}건이어야 함 — {detail})"
        empty = [m["title"][:30] for m in members if not m["subtopics"]]
        if empty:
            return False, f"소주제 없는 기사 {len(empty)}건: {', '.join(empty)}"
        return True, f"묶음 {n}건"

    return False, f"알 수 없는 슬롯 '{slot}'"


def _write_states(
    slots: dict[str, list[dict]],
    wait: list[dict],
    exclude: list[dict],
    unassigned: list[dict],
    results: dict[str, tuple[bool, str]],
) -> int:
    """보드 카드에 판정 문구를 써 넣는다. 쓴 개수를 반환한다.

    같은 슬롯의 카드는 모두 같은 문구를 받는다. 어느 카드를 보든
    이 묶음이 승인 가능한지 알 수 있어야 하기 때문이다.
    """
    written = 0

    for slot, members in slots.items():
        passed, _reason = results[slot]
        state = BUNDLE_STATE_OK if passed else BUNDLE_STATE_NG
        for m in members:
            try:
                set_bundle_state(m["page_id"], state)
                written += 1
            except Exception as e:
                log.warning(f"판정 문구 기록 실패 ({m['title'][:30]}): {e}")

    for it, state in [(x, BUNDLE_STATE_WAIT) for x in wait] + [
        (x, BUNDLE_STATE_EXCLUDE) for x in exclude
    ]:
        try:
            set_bundle_state(it["page_id"], state)
            written += 1
        except Exception as e:
            log.warning(f"판정 문구 기록 실패 ({it['title'][:30]}): {e}")

    for it in unassigned:
        try:
            set_bundle_state(it["page_id"], BUNDLE_STATE_NONE)
            written += 1
        except Exception as e:
            log.warning(f"판정 문구 기록 실패 ({it['title'][:30]}): {e}")

    return written


def _next_bundle_ids(existing: list[str], count: int) -> list[str]:
    """오늘 날짜로 다음 묶음ID 를 만든다. ['20260901-03', '20260901-04', ...]

    이미 '초안대기'에 있는 같은 날짜 번호를 이어받는다. 하루에 confirm 을
    두 번 돌려도 앞 회차와 부딪히지 않는다.
    """
    today = f"{datetime.now(KST):%Y%m%d}"
    used = set()
    for bid in existing:
        head, _, tail = bid.partition("-")
        if head == today and tail.isdigit():
            used.add(int(tail))

    out: list[str] = []
    n = 0
    while len(out) < count:
        n += 1
        if n in used:
            continue
        out.append(f"{today}-{n:02d}")
    return out


def run(*, dry_run: bool = False, check_only: bool = False) -> None:
    setup()
    log.info("=" * 50)
    if check_only:
        log.info("배치 점검 (판정 문구만 기록)")
    else:
        log.info("묶음 확정 시작" + (" [DRY-RUN]" if dry_run else ""))
    log.info("=" * 50)

    try:
        news_ds = resolve_data_source_id()

        # 스키마를 먼저 맞춘다. '대기'·'제외' 옵션 추가와
        # '콘텐츠연결' -> '초안대기' 개명이 여기서 일어난다.
        ensure_schema(news_ds)

        items = fetch_by_status(news_ds, STATUS_DEFAULT)
        if not items:
            log.info(f"'{STATUS_DEFAULT}' 기사가 없습니다. 먼저 content 를 실행하세요.")
            return

        # 슬롯별로 모은다
        slots: dict[str, list[dict]] = {}
        wait: list[dict] = []      # '대기' — 짝 부족. 상태를 건드리지 않는다
        exclude: list[dict] = []   # '제외' — 사람이 뺐다. '보류'로 넘긴다
        legacy: list[dict] = []    # 옛 '보류' 칸. 의도를 알 수 없어 건드리지 않는다
        unassigned: list[dict] = []

        for it in items:
            slot = it.get("bundle", "")
            if not slot:
                unassigned.append(it)
            elif slot == BUNDLE_WAIT:
                wait.append(it)
            elif slot == BUNDLE_EXCLUDE:
                exclude.append(it)
            elif slot == BUNDLE_LEGACY_HOLD:
                legacy.append(it)
            else:
                slots.setdefault(slot, []).append(it)

        log.info(
            f"배치 현황: {len(slots)}개 슬롯 · 대기 {len(wait)}건 "
            f"· 제외 {len(exclude)}건 · 미배정 {len(unassigned)}건"
        )

        if legacy:
            # 옛 '보류' 칸이 '대기'와 '제외'로 갈라졌다. 남아 있는 카드는
            # 코드가 넣은 것인지 사람이 옮긴 것인지 구분할 수 없으므로
            # 상태를 바꾸지 않는다. 사람이 보드에서 옮겨 주어야 한다.
            log.warning(
                f"옛 '{BUNDLE_LEGACY_HOLD}' 칸에 {len(legacy)}건이 남아 있습니다. "
                f"'{BUNDLE_WAIT}' 또는 '{BUNDLE_EXCLUDE}' 로 옮겨 주세요 "
                f"(이번 실행에서는 건드리지 않습니다)."
            )
            for it in legacy:
                log.warning(f"      - {it['title'][:52]}")

        # '대기'만 있는 경우에도 계속 진행한다. --check 로 판정 칩을 새로
        # 찍어야 보드에서 어느 카드가 왜 남아 있는지 보인다.
        if not slots and not exclude and not wait:
            log.info("확정할 배치가 없습니다. Notion 보드에서 카드를 배치하세요.")
            return

        # 검사 — 본문은 통과한 슬롯에서만 읽는다 (읽기 비용이 크다)
        results: dict[str, tuple[bool, str]] = {}
        ok_slots: list[tuple[str, list[dict], str]] = []
        for slot in sorted(slots):
            members = slots[slot]
            passed, reason = _validate(slot, members)
            results[slot] = (passed, reason)
            mark = "OK " if passed else "거부"
            log.info(f"[{mark}] {slot} — {reason}")
            for m in members:
                log.info(f"      - {m['title'][:52]}")
            if passed:
                ok_slots.append((slot, members, reason))

        rejected = len(slots) - len(ok_slots)
        if rejected:
            log.warning(f"{rejected}개 슬롯이 규칙에 맞지 않습니다.")
            log.warning("보드에서 배치를 고친 뒤 다시 확인하세요.")

        # ── 점검 모드: 판정 문구만 쓰고 끝낸다 ────────────
        if check_only:
            written = _write_states(slots, wait, exclude, unassigned, results)
            log.info(
                f"판정 기록 {written}건 — 승인 가능 {len(ok_slots)}편 · "
                f"불가 {rejected}개 · 대기 {len(wait)}건 · 제외 {len(exclude)}건 "
                f"· 미배정 {len(unassigned)}건"
            )
            log.info("보드에서 '묶음상태' 를 확인하세요. Content 는 만들지 않았습니다.")
            return

        if dry_run:
            log.info(
                f"[dry-run] 확정 가능 {len(ok_slots)}편 · 거부 {rejected}개 · "
                f"제외 {len(exclude)}건 · 대기 {len(wait)}건 "
                f"— 아무것도 바꾸지 않았습니다."
            )
            return

        if not ok_slots and not exclude:
            log.warning("확정할 수 있는 슬롯이 없습니다.")
            return

        # 이미 확정된 묶음ID 를 읽어 번호를 이어받는다.
        # 하루에 confirm 을 두 번 돌려도 앞 회차와 부딪히지 않는다.
        pending = fetch_by_status(news_ds, STATUS_LINKED)
        bundle_ids = _next_bundle_ids(
            [p.get("bundle_id", "") for p in pending], len(ok_slots)
        )

        confirmed = 0
        for (slot, members, reason), bid in zip(ok_slots, bundle_ids):
            # 슬롯 비우기 + 묶음ID 부여 + '초안대기'를 한 번의 PATCH 로 보낸다.
            # 한 기사라도 실패하면 그 묶음은 통째로 되돌린다. 절반만 넘어가면
            # publish 가 3건짜리 묶음을 만들어 분량이 어긋난다.
            done: list[str] = []
            try:
                for m in members:
                    confirm_bundle(m["page_id"], bid)
                    done.append(m["page_id"])
            except Exception as e:
                log.warning(f"확정 실패 [{slot}] {bid}: {e}")
                for pid in done:
                    try:
                        # 슬롯은 애초에 건드리지 않았으므로 그대로 둔다
                        set_bundle_id(pid, None)
                        update_status(pid, STATUS_DEFAULT)
                    except Exception as e2:
                        log.error(f"되돌리기 실패 ({pid[:8]}): {e2}")
                continue

            confirmed += 1
            log.info(f"확정 [{slot}] -> {bid} · {len(members)}건 — {reason}")

        # '제외' 칸만 상태를 '보류'로 옮긴다. 후보 목록에서 빠지고,
        # 되살리려면 Notion 에서 '선정됨'으로 되돌리면 된다.
        for it in exclude:
            try:
                update_status(it["page_id"], STATUS_HOLD)
                log.info(f"제외 처리: {it['title'][:44]}")
            except Exception as e:
                log.warning(f"제외 처리 실패 ({it['title'][:30]}): {e}")

        # '대기' 칸은 손대지 않는다. '선정됨'으로 남아야 다음 실행에서
        # 새로 들어온 기사와 다시 묶일 수 있다.
        if wait:
            log.info(f"대기 {len(wait)}건은 '{STATUS_DEFAULT}' 로 남겨 둡니다 (다음 회차 재시도)")

        log.info(
            f"확정 완료: {confirmed}편 · 제외 {len(exclude)}건 · "
            f"대기 {len(wait)}건 · 거부 {rejected}개"
        )
        if confirmed:
            log.info(f"{confirmed}편이 '{STATUS_LINKED}' 로 넘어갔습니다.")
            log.info("보드 필터에 '초안대기'를 넣으면 확정된 카드가 그대로 보입니다.")
            log.info("초안을 만들려면 `run.py publish` 를 실행하세요.")
            log.info("확정을 취소하려면 보드에서 카드를 '선정됨'으로 되돌리면 됩니다.")
        log.info("파이프라인 종료")

    except Exception as e:
        log.exception("묶음 확정 파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("묶음 확정", e)
        raise
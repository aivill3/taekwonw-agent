"""묶음 확정 파이프라인 (보드 배치를 굳힌다).

흐름:
  '선정됨' 조회 → '묶음' 슬롯별로 모음 → 규칙 검사
  → 통과한 슬롯에 묶음ID 부여 (상태는 '초안요청' 그대로)

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

  버튼 전   묶음=단독1   묶음ID=(빈값)       상태=선정됨
  버튼 후   묶음=단독1   묶음ID=(빈값)       상태=초안요청
  확정 후   묶음=단독1   묶음ID=20260902-01  상태=초안요청   ← 보드에 남음
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

규칙을 어긴 슬롯은 통째로 건너뛴다. 카드는 '선정됨' 으로 돌아가고
슬롯은 '대기' 로 비워져, 다음 content 실행에서 새 기사와 다시 묶인다.

대기와 제외를 나눈 이유
---------------------
예전에는 '보류' 한 칸이었고 confirm 이 그 칸의 카드를 전부 '보류'
상태로 넘겼다. 그런데 그 칸에는 성격이 다른 둘이 섞여 있었다.

  content 가 넣은 것 — 4건을 못 채웠을 뿐. 다음 회차에 다시 묶여야 한다.
  사람이 옮긴 것     — 발행하지 않기로 한 것.

앞의 것까지 '보류'로 넘어가면서, 짝만 기다리면 될 기사가 후보에서
사라졌다. 그래서 칸을 '대기'(코드)와 '제외'(사람)로 나눴다.

판정은 카드가 아니라 콜아웃에 적는다
--------------------------------
2026-09-14 이전에는 카드마다 '묶음상태' 칩(✅ 승인 가능 / ⚠️ 승인 불가 /
⏳ 대기 / 🚫 제외 / ◻️ 미배정)을 붙였다. 전부 걷어냈다.

다섯 중 셋은 카드가 놓인 칸과 같은 말을 두 번 하고 있었다. '대기' 칸의
카드에 '⏳ 대기' 를 붙이는 식이다. 같은 사실을 두 군데가 말하면 언젠가
어긋나고, 어긋나면 어느 쪽이 맞는지 알 방법이 없다. 실제로 어긋났다.

나머지 둘도 오래가지 못했다. 통과한 카드의 '✅ 승인 가능'은 확정되는
순간 지워야 했고(묶음ID 가 붙는 것이 곧 결과다), 거부된 카드의
'⚠️ 승인 불가'는 카드가 '대기' 로 내려가는 순간 거짓이 됐다.

그래서 역할을 나눴다.

  칸       이 카드가 지금 어디에 속하는가 (단독N · 묶음N · 대기 · 제외)
  콜아웃   이번 판정이 무엇을 어떻게 판단했는가

보드 밑 '승인 불가 현황' 콜아웃에 현황 전문을 쓴다. 이어붙이지 않고
매번 통째로 덮어쓴다 — 담아야 할 것이 '오늘 있었던 일'이 아니라 '지금
무엇을 손봐야 하는가'이기 때문이다. 지난 회차는 로그와 Slack 에 남는다.

  승인 불가 현황 — 2026-09-14 17:08 기준

  ❌ 묶음1 — 묶음 슬롯에 2건 (4건이어야 함 — 2건 부족)
      · 극동대, '감곡 K-컬처 페스티벌' 9월 12~14일 개최…유학생
      → '대기' 칸으로 내렸습니다
  ✅ 확정 2편 — 20260914-01 · 20260914-02
  ⏳ 대기 3건 · 🚫 제외 1건 · ◻️ 미배정 0건

NOTION_BOARD_PAGE_ID 가 비어 있으면 이 기록은 건너뛴다. 그 경우 판정은
로그에만 남는다.

이렇게 하는 이유는 실행을 한 번으로 끝내기 위해서다. 사람이 로그를 보지
않는다는 전제에서는, 왜 안 넘어갔는지가 보드에 남아야 한다. 예전에는
--check 로 미리 보고 confirm 으로 확정하는 2단계였는데, 버튼을 누르는
사람에게 같은 일을 두 번 시키는 셈이었다.

--check 는 '선정됨' 을 본다
-------------------------
실행은 '초안요청'(버튼이 눌린 것)을 보지만, 점검은 '선정됨'(아직 누르지
않은 배치)을 본다. 대상이 같으면 점검이 사실상 돌아가지 않는다 — 폴링이
30분마다 '초안요청' 을 집어가므로, 사람이 CLI 를 칠 무렵이면 그 카드는
이미 처리돼 목록이 비어 있다.

점검도 같은 콜아웃에 쓴다. 대신 맨 위에 한 줄을 덧붙인다.

  ※ 점검 — 아무것도 바꾸지 않았습니다

이 줄이 없으면 예고와 결과가 같은 자리에서 같은 얼굴로 보인다. 상태도
슬롯도 바꾸지 않으므로, 점검이 남기는 것은 이 글 하나뿐이다.

Notion 은 조건부 문구를 실시간으로 띄우지 못하므로, 카드를 옮긴 뒤에는
다시 실행해야 갱신된다. 칸 제목 옆의 '건수'는 Notion 이 실시간으로
보여주니 함께 보면 된다.
"""
from datetime import datetime

from config.settings import KST, NOTION_BOARD_PAGE_ID
from core.logger import get_logger, setup
from tools.slack_notifier import notify_failure
from tools.notion_store import (
    BUNDLE_EXCLUDE,
    BUNDLE_GROUP_SLOTS,
    BUNDLE_LEGACY_HOLD,
    BUNDLE_SOLO_SLOTS,
    BUNDLE_WAIT,
    STATUS_DEFAULT,
    STATUS_HOLD,
    STATUS_REQUESTED_DRAFT,
    confirm_bundle,
    ensure_schema,
    fetch_by_status,
    resolve_data_source_id,
    set_bundle,
    set_bundle_id,
    write_notice,
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


def _next_bundle_ids(existing: list[str], count: int) -> list[str]:
    """오늘 날짜로 다음 묶음ID 를 만든다. ['20260901-03', '20260901-04', ...]

    이미 확정된 같은 날짜 번호를 이어받는다. 하루에 confirm 을
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


def _status_body(
    slots: dict[str, list[dict]],
    results: dict[str, tuple[bool, str]],
    wait: list[dict],
    exclude: list[dict],
    unassigned: list[dict],
    *,
    check_only: bool,
    confirmed: list[str] | None = None,
) -> str:
    """콜아웃에 쓸 현황 전문(全文)을 만든다.

    칩을 걷어낸 뒤로 이 글이 판정을 읽을 유일한 창구다. 그래서 '거부 사유'
    만이 아니라 이번 판정의 전부를 담는다 — 무엇이 막혔는지, 무엇이
    통과했는지, 나머지가 어느 칸에 몇 건 있는지.

    순서는 손댈 일이 있는 것부터다. 거부 → 통과 → 그 외 칸의 건수.
    보드를 여는 사람이 맨 위 몇 줄만 읽고도 오늘 할 일을 알 수 있어야 한다.

    점검(--check)일 때는 맨 위에 한 줄을 덧붙인다. 같은 콜아웃을 쓰므로,
    이 글이 예고인지 결과인지 구분되지 않으면 보드가 거짓말을 한다.

        ※ 점검 — 아무것도 바꾸지 않았습니다

        ❌ 묶음1 — 묶음 슬롯에 2건 (4건이어야 함 — 2건 부족)
            · 극동대, '감곡 K-컬처 페스티벌' 9월 12~14일 개최…유학생
            → '대기' 칸으로 내렸습니다
        ✅ 확정 2편 — 20260914-01 · 20260914-02
        ⏳ 대기 3건 · 🚫 제외 1건 · ◻️ 미배정 0건
    """
    lines: list[str] = []
    if check_only:
        lines += ["※ 점검 — 아무것도 바꾸지 않았습니다", ""]

    rejected = {k: v for k, v in slots.items() if not results[k][0]}
    for slot in sorted(rejected):
        lines.append(f"❌ {slot} — {results[slot][1]}")
        for m in rejected[slot]:
            # 44자에서 자른다. 제목이 길면 콜아웃이 화면을 넘어간다.
            lines.append(f"    · {m['title'][:44]}")
        if not check_only:
            # 카드가 어디로 갔는지까지 적는다. 보드에서 카드를 찾지 못해
            # '사라졌다'고 오해하는 것을 막는다.
            lines.append(f"    → '{BUNDLE_WAIT}' 칸으로 내렸습니다")

    passed = sorted(k for k in slots if results[k][0])
    if check_only:
        if passed:
            lines.append(f"✅ 승인 가능 {len(passed)}편 — {' · '.join(passed)}")
    elif confirmed:
        lines.append(f"✅ 확정 {len(confirmed)}편 — {' · '.join(confirmed)}")

    lines.append(
        f"⏳ {BUNDLE_WAIT} {len(wait)}건 · 🚫 {BUNDLE_EXCLUDE} {len(exclude)}건 "
        f"· ◻️ 미배정 {len(unassigned)}건"
    )

    if not rejected and not passed:
        lines.append("손댈 것이 없습니다.")

    return "\n".join(lines)


def _publish_status(page_id: str, body: str) -> None:
    """현황을 콜아웃에 쓴다. 실패해도 판정 결과를 무르지 않는다.

    보드 페이지 ID 가 설정돼 있지 않으면 조용히 건너뛴다 — 콜아웃은
    사람이 읽는 창구이고, 판정 자체는 로그와 Slack 에도 남는다.
    """
    if not page_id:
        return
    try:
        write_notice(page_id, body)
    except Exception as e:
        log.warning(f"현황 기록 실패: {e}")


def run(*, dry_run: bool = False, check_only: bool = False) -> None:
    setup()
    log.info("=" * 50)
    if check_only:
        log.info(f"배치 점검 — '{STATUS_DEFAULT}' 대상 (판정 칩만 기록)")
    else:
        log.info("묶음 확정 시작" + (" [DRY-RUN]" if dry_run else ""))
    log.info("=" * 50)

    try:
        news_ds = resolve_data_source_id()

        # 스키마를 먼저 맞춘다. '대기'·'제외' 옵션 추가와
        # '초안요청' 옵션 추가가 여기서 일어난다.
        ensure_schema(news_ds)

        # 실행은 사람이 [초안 작성] 을 눌러 '초안요청' 으로 바꾼 것만 본다.
        # 점검은 반대로 아직 누르지 않은 '선정됨' 을 본다 — 같은 대상을 보면
        # 폴링이 이미 집어간 뒤라 목록이 비어 점검이 돌아가지 않는다.
        target_status = STATUS_DEFAULT if check_only else STATUS_REQUESTED_DRAFT
        items = fetch_by_status(news_ds, target_status)
        if not items:
            log.info(f"'{target_status}' 기사가 없습니다. 판정할 것이 없습니다.")
            return

        # 슬롯별로 모은다
        slots: dict[str, list[dict]] = {}
        done: list[dict] = []      # 이미 묶음ID 가 붙은 것
        wait: list[dict] = []      # '대기' — 짝 부족. 상태를 건드리지 않는다
        exclude: list[dict] = []   # '제외' — 사람이 뺐다. '보류'로 넘긴다
        legacy: list[dict] = []    # 옛 '보류' 칸. 의도를 알 수 없어 건드리지 않는다
        unassigned: list[dict] = []

        for it in items:
            # 이미 확정된 것. publish 가 처리할 차례라 여기서는 건드리지 않는다.
            # (폴링이 30분마다 도는데 매번 다시 검사할 이유가 없다)
            if it.get("bundle_id"):
                done.append(it)
                continue

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
            f"배치 현황: {len(slots)}개 슬롯 · 확정됨 {len(done)}건 · "
            f"대기 {len(wait)}건 · 제외 {len(exclude)}건 · 미배정 {len(unassigned)}건"
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

        if not slots and not exclude and not wait and not unassigned:
            log.info("새로 확정할 배치가 없습니다.")
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

        # ── 점검 모드: 콜아웃에 예고만 쓰고 끝낸다 ────────
        #    상태도 슬롯도 바꾸지 않는다. 콜아웃 맨 위의 '※ 점검' 줄이
        #    이 글을 결과가 아니라 예고로 읽게 한다.
        if check_only:
            _publish_status(
                NOTION_BOARD_PAGE_ID,
                _status_body(
                    slots, results, wait, exclude, unassigned, check_only=True
                ),
            )
            log.info(
                f"점검 완료 — 승인 가능 {len(ok_slots)}편 · "
                f"불가 {rejected}개 · 대기 {len(wait)}건 · 제외 {len(exclude)}건 "
                f"· 미배정 {len(unassigned)}건"
            )
            log.info(
                "보드 밑 '승인 불가 현황' 을 확인하세요. 아무것도 확정하지 "
                "않았습니다 — 실제로 넘기려면 [초안 작성] 을 누르세요."
            )
            return

        if dry_run:
            log.info(
                f"[dry-run] 확정 가능 {len(ok_slots)}편 · 거부 {rejected}개 · "
                f"제외 {len(exclude)}건 · 대기 {len(wait)}건 "
                f"— 아무것도 바꾸지 않았습니다."
            )
            return

        if not ok_slots and not exclude:
            log.warning("확정할 수 있는 슬롯이 없습니다. 거부된 배치는 되돌립니다.")

        confirmed = 0
        confirmed_ids: list[str] = []   # 콜아웃에 적을 확정 묶음ID
        if ok_slots:
            # 오늘 이미 쓴 묶음ID 를 피해 번호를 이어받는다.
            bundle_ids = _next_bundle_ids(
                [d.get("bundle_id", "") for d in done], len(ok_slots)
            )

            for (slot, members, reason), bid in zip(ok_slots, bundle_ids):
                # 묶음ID 부여. 상태는 '초안요청' 그대로다.
                # 한 기사라도 실패하면 그 묶음은 통째로 되돌린다. 절반만
                # 확정되면 publish 가 3건짜리 묶음을 만들어 분량이 어긋난다.
                ok: list[str] = []
                try:
                    for m in members:
                        confirm_bundle(m["page_id"], bid)
                        ok.append(m["page_id"])
                except Exception as e:
                    log.warning(f"확정 실패 [{slot}] {bid}: {e}")
                    for pid in ok:
                        try:
                            set_bundle_id(pid, None)
                        except Exception as e2:
                            log.error(f"되돌리기 실패 ({pid[:8]}): {e2}")
                    continue

                confirmed += 1
                confirmed_ids.append(bid)
                log.info(f"확정 [{slot}] -> {bid} · {len(members)}건 — {reason}")

        # 규칙을 어긴 칸은 '선정됨' 으로 되돌리고 '대기' 로 옮긴다.
        #
        # '초안요청' 으로 두면 폴링이 30분마다 같은 검사를 반복하고,
        # 사람은 보드에서 그 카드를 다시 만질 수 없다(필터가 '선정됨'이라
        # 화면에서 사라진다). 되돌리면 제자리로 돌아온다.
        #
        # 슬롯까지 비우는 이유
        # -------------------
        # 예전에는 슬롯(예: 묶음1)에 그대로 두고 카드에 '⚠️ 승인 불가'
        # 칩만 붙였다. 그런데 2건짜리가 '묶음1' 에 남아 있으면 보드가
        # 거짓말을 한다 — 그 칸은 '이 4건이 한 편이 된다' 는 뜻이다.
        # 짝이 부족한 것은 대기해야 할 것이지 묶여 있을 것이 아니다.
        #
        # 대기로 내려가면 다음 content 실행에서 group_articles() 가
        # 새로 들어온 기사와 함께 다시 묶는다. 4건이 채워지면 정상
        # 슬롯으로 올라가고, 그때까지는 대기에 머문다. 슬롯이 열 칸뿐이라
        # 거부된 카드가 계속 붙들고 있으면 다음 배정이 막히기도 한다.
        rejected_slots = {
            slot: members for slot, members in slots.items() if not results[slot][0]
        }

        reverted = 0
        moved: list[dict] = []
        for slot, members in rejected_slots.items():
            for m in members:
                try:
                    update_status(m["page_id"], STATUS_DEFAULT)
                    set_bundle(m["page_id"], BUNDLE_WAIT)
                    moved.append(m)
                    reverted += 1
                except Exception as e:
                    log.warning(f"되돌리기 실패 ({m['title'][:30]}): {e}")

        # 슬롯이 없거나 이미 '대기' 칸인 것도 마찬가지로 되돌린다.
        # 버튼이 눌렸지만 아직 배치가 끝나지 않은 카드들이다.
        for it in wait + unassigned:
            try:
                update_status(it["page_id"], STATUS_DEFAULT)
                reverted += 1
            except Exception as e:
                log.warning(f"되돌리기 실패 ({it['title'][:30]}): {e}")

        # '제외' 칸만 상태를 '보류'로 옮긴다. 후보 목록에서 빠지고,
        # 되살리려면 Notion 에서 '선정됨'으로 되돌리면 된다.
        for it in exclude:
            try:
                update_status(it["page_id"], STATUS_HOLD)
                log.info(f"제외 처리: {it['title'][:44]}")
            except Exception as e:
                log.warning(f"제외 처리 실패 ({it['title'][:30]}): {e}")

        # 보드 밑 콜아웃에 이번 판정의 전부를 쓴다. 카드에 칩이 없으므로
        # 여기가 사람이 판정을 읽을 유일한 창구다. 거부가 없어도 쓴다 —
        # 비어 있으면 '아직 안 돌았다' 와 구분되지 않는다.
        _publish_status(
            NOTION_BOARD_PAGE_ID,
            _status_body(
                slots, results, wait, exclude, unassigned,
                check_only=False, confirmed=confirmed_ids,
            ),
        )

        log.info(
            f"확정 {confirmed}편 · 제외 {len(exclude)}건 · "
            f"거부·미배치 {reverted}건은 '{STATUS_DEFAULT}' 로 되돌림"
        )
        if confirmed:
            log.info("이어서 초안을 작성합니다 (`run.py publish --requested`).")
        if reverted:
            log.info("되돌린 카드는 보드 밑 '승인 불가 현황' 을 확인한 뒤 다시 눌러주세요.")
        log.info("파이프라인 종료")

    except Exception as e:
        log.exception("묶음 확정 파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("묶음 확정", e)
        raise
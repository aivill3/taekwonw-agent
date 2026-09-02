"""밀린 '선정됨' 기사 정리 (기사를 지우지 않고 상태만 바꾼다).

왜 필요한가
----------
승인이 며칠 밀리면 '선정됨'이 계속 쌓인다. 실측(2026-08-28)으로 43건이
모여 있었고, 필터·중복 제거를 거쳐도 16편이 남았다. Gemini 2.0 Flash 는
RPD 20이라 재시도까지 감안하면 하루 10편이 실질 상한이다.

또 오래된 단신은 지금 써도 시의성이 없다. '어제 열린 대회 성료' 기사를
일주일 뒤에 올릴 이유가 없다.

무엇을 하는가
------------
조건에 맞지 않는 기사의 상태를 '선정됨' → '보류' 로 바꾼다.
기사도 본문도 지우지 않는다. Notion 에서 상태만 되돌리면 다시 후보가 된다.
(--restore 로 한꺼번에 되돌릴 수도 있다)

기본은 미리보기다. 실제로 바꾸려면 --apply 를 붙여야 한다.

사용법
-----
    # 지금 상태 확인 (아무것도 바꾸지 않는다)
    uv run python -m tools.cleanup_selected

    # 최근 2일치만 남기고 나머지는 보류
    uv run python -m tools.cleanup_selected --days 2 --apply

    # 점수 상위 8건만 남기고 나머지는 보류
    uv run python -m tools.cleanup_selected --keep 8 --apply

    # 둘 다: 2일 이내 중에서 상위 8건
    uv run python -m tools.cleanup_selected --days 2 --keep 8 --apply

    # 보류한 것을 전부 '선정됨'으로 되돌린다
    uv run python -m tools.cleanup_selected --restore --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import KST  # noqa: E402
from core.logger import setup  # noqa: E402
from tools.notion_store import (  # noqa: E402
    PROP_DATE,
    PROP_MODE,
    PROP_SCORE,
    PROP_STATUS,
    PROP_SUBTOPICS,
    STATUS_DEFAULT,
    STATUS_HOLD,
    _plain_text,
    _request,
    resolve_data_source_id,
    update_status,
)


def fetch_detail(data_source_id: str, status: str) -> list[dict]:
    """정리 판단에 필요한 속성까지 함께 읽는다.

    fetch_by_status() 는 날짜·점수를 돌려주지 않는다. 여기서는 '얼마나
    오래됐는지'와 '점수가 얼마인지'가 판단 기준이라 직접 질의한다.
    """
    body = {
        "filter": {"property": PROP_STATUS, "select": {"equals": status}},
        "page_size": 100,
    }
    items: list[dict] = []
    cursor = None

    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _request("POST", f"/data_sources/{data_source_id}/query", json=body)

        for page in data.get("results", []):
            props = page.get("properties", {})

            title = ""
            for prop in props.values():
                if prop.get("type") == "title":
                    title = _plain_text(prop)
                    break

            date_prop = (props.get(PROP_DATE) or {}).get("date") or {}
            mode_prop = (props.get(PROP_MODE) or {}).get("select") or {}

            items.append({
                "page_id": page["id"],
                "title": title,
                "date": date_prop.get("start", "") or page.get("created_time", ""),
                "score": (props.get(PROP_SCORE) or {}).get("number") or 0,
                "mode": mode_prop.get("name", ""),
                "subtopics": [
                    _plain_text(props[n]) for n in PROP_SUBTOPICS
                    if n in props and _plain_text(props[n])
                ],
            })

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return items


def age_days(iso: str) -> int:
    """기사 날짜로부터 지난 일수. 파싱 실패 시 999 (아주 오래된 것으로 본다)."""
    if not iso:
        return 999
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 999
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return max(0, (datetime.now(KST) - dt.astimezone(KST)).days)


def split(items: list[dict], days: int | None, keep: int | None) -> tuple[list, list]:
    """(남길 것, 보류할 것) 으로 나눈다.

    날짜를 먼저 적용하고 그 안에서 점수 상위를 남긴다. 순서가 반대면
    오래된 고득점 기사가 최근 기사를 밀어낸다.
    """
    survivors = list(items)
    dropped: list[tuple[dict, str]] = []

    if days is not None:
        fresh, stale = [], []
        for it in survivors:
            (fresh if it["age"] <= days else stale).append(it)
        dropped += [(it, f"{it['age']}일 지남 (기준 {days}일)") for it in stale]
        survivors = fresh

    if keep is not None and len(survivors) > keep:
        survivors.sort(key=lambda x: (-x["score"], x["age"]))
        cut = survivors[keep:]
        dropped += [(it, f"점수 {it['score']:g}, 상위 {keep}건 밖") for it in cut]
        survivors = survivors[:keep]

    survivors.sort(key=lambda x: (x["age"], -x["score"]))
    return survivors, dropped


def show(items: list[dict], title: str) -> None:
    if not items:
        return
    print(f"\n{title} ({len(items)}건)")
    print(f"  {'경과':>4} {'점수':>5} {'구분':>4}  제목")
    for it in items:
        print(f"  {it['age']:>3}일 {it['score']:>5.0f} {it['mode']:>4}  {it['title'][:58]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="밀린 '선정됨' 기사 정리 (상태만 변경)")
    ap.add_argument("--days", type=int, default=None,
                    help="이 일수 이내 기사만 남긴다 (예: 2 = 오늘·어제)")
    ap.add_argument("--keep", type=int, default=None,
                    help="점수 상위 N건만 남긴다")
    ap.add_argument("--restore", action="store_true",
                    help="'보류' 기사를 '선정됨'으로 되돌린다")
    ap.add_argument("--apply", action="store_true",
                    help="실제로 상태를 바꾼다 (없으면 미리보기만)")
    args = ap.parse_args()

    setup()
    ds = resolve_data_source_id()

    # ── 되돌리기 ───────────────────────────────────
    if args.restore:
        items = fetch_detail(ds, STATUS_HOLD)
        for it in items:
            it["age"] = age_days(it["date"])
        if not items:
            print(f"'{STATUS_HOLD}' 기사가 없습니다.")
            return 0

        show(items, f"'{STATUS_HOLD}' → '{STATUS_DEFAULT}' 되돌릴 기사")
        if not args.apply:
            print(f"\n미리보기입니다. 실제로 되돌리려면 --apply 를 붙이세요.")
            return 0

        done = 0
        for it in items:
            try:
                update_status(it["page_id"], STATUS_DEFAULT)
                done += 1
            except Exception as e:
                print(f"  실패: {it['title'][:40]} — {e}")
        print(f"\n{done}건 되돌렸습니다.")
        return 0

    # ── 정리 ───────────────────────────────────────
    items = fetch_detail(ds, STATUS_DEFAULT)
    for it in items:
        it["age"] = age_days(it["date"])

    if not items:
        print(f"'{STATUS_DEFAULT}' 기사가 없습니다.")
        return 0

    print("=" * 70)
    print(f"'{STATUS_DEFAULT}' {len(items)}건")
    if args.days is None and args.keep is None:
        print("  조건이 없어 현황만 표시합니다. --days 또는 --keep 를 지정하세요.")
    print("=" * 70)

    # 날짜 분포를 먼저 보여준다. 며칠치가 밀렸는지 한눈에 보인다.
    dist: dict[int, int] = {}
    for it in items:
        dist[it["age"]] = dist.get(it["age"], 0) + 1
    print("\n[날짜 분포]")
    for d in sorted(dist):
        label = "오늘" if d == 0 else f"{d}일 전"
        print(f"  {label:>7}: {'#' * dist[d]} {dist[d]}건")

    if args.days is None and args.keep is None:
        items.sort(key=lambda x: (x["age"], -x["score"]))
        show(items, "전체 목록")
        print("\n예) 최근 2일치만 남기기:  --days 2 --apply")
        print("    점수 상위 8건만 남기기: --keep 8 --apply")
        return 0

    survivors, dropped = split(items, args.days, args.keep)

    show(survivors, "남길 기사")
    if dropped:
        print(f"\n'{STATUS_HOLD}' 로 넘길 기사 ({len(dropped)}건)")
        print(f"  {'경과':>4} {'점수':>5}  제목 / 사유")
        for it, reason in sorted(dropped, key=lambda x: x[0]["age"]):
            print(f"  {it['age']:>3}일 {it['score']:>5.0f}  {it['title'][:52]}")
            print(f"{'':>13}   └ {reason}")

    print()
    print("=" * 70)
    print(f"남김 {len(survivors)}건 · 보류 {len(dropped)}건")

    if not args.apply:
        print("\n미리보기입니다. 실제로 바꾸려면 --apply 를 붙이세요.")
        print("(기사는 지워지지 않습니다. --restore 로 되돌릴 수 있습니다)")
        return 0

    done = 0
    for it, _reason in dropped:
        try:
            update_status(it["page_id"], STATUS_HOLD)
            done += 1
        except Exception as e:
            print(f"  실패: {it['title'][:40]} — {e}")

    print(f"\n{done}건을 '{STATUS_HOLD}' 로 넘겼습니다.")
    print("이제 `uv run python run.py content --dry-run` 으로 편수를 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
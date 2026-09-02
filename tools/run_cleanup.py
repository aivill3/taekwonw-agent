"""보관 정리 실행 도구 (오래된 Notion 페이지를 휴지통으로 옮긴다).

왜 필요한가
----------
notion_store.cleanup_expired() 는 만들어져 있지만 어느 워크플로도 호출하지
않는다. 그래서 페이지가 계속 쌓인다.

  실측(2026-09-01): 8월 3일부터 9월 1일까지 20일치가 그대로 남아 있었다.

이 도구는 그 함수를 사람이 직접 부를 수 있게 감싼 것이다. 완전 삭제가
아니라 휴지통 이동이라 30일간 복구할 수 있다.

기준이 '기사 날짜'가 아니라 '페이지 생성 시각'이다
------------------------------------------------
cleanup_expired() 는 created_time 을 본다. Notion 화면에서 날짜별로 묶어
보고 있다면 그건 '날짜'(기사 발행일) 속성일 가능성이 높아, 보이는 것과
정리 기준이 다를 수 있다. --list 로 두 값을 나란히 확인할 수 있다.

'작성완료'가 안 지워지는 이유
---------------------------
config.KEEP_WRITTEN 이 True 면 작성완료는 정책에서 아예 빠진다. 블로그
원고가 담겨 있어 기본이 보존이다. 지우려면 설정을 False 로 바꾸고
WRITTEN_RETENTION_DAYS 를 정해야 한다.

사용법
-----
    # 현재 정책과 상태별 건수 확인 (아무것도 안 지움)
    uv run python -m tools.run_cleanup

    # 무엇이 지워질지 미리보기
    uv run python -m tools.run_cleanup --dry-run

    # 실제 정리
    uv run python -m tools.run_cleanup --apply

    # 상태별 목록을 생성일과 함께 보기
    uv run python -m tools.run_cleanup --list
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (  # noqa: E402
    KEEP_WRITTEN,
    KST,
    RETENTION_DAYS,
    WRITTEN_RETENTION_DAYS,
)
from core.logger import setup  # noqa: E402
from tools.notion_store import (  # noqa: E402
    STATUS_COLLECTED,
    STATUS_DEFAULT,
    STATUS_HOLD,
    STATUS_LINKED,
    STATUS_REQUESTED,
    STATUS_WRITTEN,
    cleanup_expired,
    fetch_by_status,
    resolve_data_source_id,
)

ALL_STATUSES = [
    STATUS_COLLECTED,
    STATUS_REQUESTED,
    STATUS_DEFAULT,
    STATUS_LINKED,
    STATUS_WRITTEN,
    STATUS_HOLD,
]


def age_days(iso: str) -> int:
    if not iso:
        return -1
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return -1
    return max(0, (datetime.now(KST) - dt.astimezone(KST)).days)


def show_policy() -> None:
    print("=" * 62)
    print("보관 정책 (config/settings.py)")
    print("=" * 62)
    for status in ALL_STATUSES:
        days = RETENTION_DAYS.get(status)
        if days is None:
            if status == STATUS_WRITTEN and KEEP_WRITTEN:
                print(f"  {status:10} 영구 보존 (KEEP_WRITTEN=True)")
            else:
                print(f"  {status:10} 정책 없음 — 정리되지 않음")
        else:
            print(f"  {status:10} {days}일 경과 시 휴지통")
    if not KEEP_WRITTEN:
        print(f"  {STATUS_WRITTEN:10} {WRITTEN_RETENTION_DAYS}일 (KEEP_WRITTEN=False)")
    print()
    print("  ※ 기준은 페이지 '생성 시각'이며, 기사 발행일이 아닙니다.")


def show_counts(ds: str, detail: bool) -> None:
    print("=" * 62)
    print("상태별 현황")
    print("=" * 62)
    total = 0
    for status in ALL_STATUSES:
        items = fetch_by_status(ds, status)
        total += len(items)
        ages = [age_days(it.get("created", "")) for it in items]
        ages = [a for a in ages if a >= 0]
        span = f"  (생성 {min(ages)}~{max(ages)}일 전)" if ages else ""
        days = RETENTION_DAYS.get(status)
        over = sum(1 for a in ages if days is not None and a >= days)
        mark = f"  → 정리 대상 {over}건" if over else ""
        print(f"  {status:10} {len(items):>4}건{span}{mark}")

        if detail and items:
            for it in sorted(items, key=lambda x: -age_days(x.get("created", ""))):
                a = age_days(it.get("created", ""))
                print(f"       {a:>3}일 전  {it['title'][:52]}")
    print(f"\n  합계 {total}건")


def main() -> int:
    ap = argparse.ArgumentParser(description="보관 기간이 지난 페이지 정리")
    ap.add_argument("--dry-run", action="store_true", help="무엇이 지워질지만 출력")
    ap.add_argument("--apply", action="store_true", help="실제로 휴지통으로 옮긴다")
    ap.add_argument("--list", action="store_true", help="상태별 목록을 생성일과 함께 출력")
    args = ap.parse_args()

    setup()
    ds = resolve_data_source_id()

    show_policy()
    print()
    show_counts(ds, args.list)
    print()

    if not args.dry_run and not args.apply:
        print("현황만 표시했습니다.")
        print("  무엇이 지워질지 보려면: --dry-run")
        print("  실제로 정리하려면:      --apply")
        return 0

    print("=" * 62)
    print("정리 " + ("미리보기" if args.dry_run and not args.apply else "실행"))
    print("=" * 62)
    removed = cleanup_expired(ds, dry_run=not args.apply)

    if not removed:
        return 0
    if not args.apply:
        print("\n미리보기입니다. 실제로 옮기려면 --apply 를 붙이세요.")
        print("(완전 삭제가 아니라 휴지통 이동이라 30일간 복구할 수 있습니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
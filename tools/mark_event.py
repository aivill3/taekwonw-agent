#!/usr/bin/env python
"""사건 이력(state.written_events) 수동 관리 — 이미 쓴 사건을 운영자가 직접 막는다.

    py tools/mark_event.py list                               이력 보기
    py tools/mark_event.py add "국제대회 3종"                 최근 수집 CSV 에서 제목 검색 → 등록
    py tools/mark_event.py add "WT 본부" --pick 1 3           검색 결과 중 1·3번만 등록
    py tools/mark_event.py add "WT 본부" --all                검색 결과 전부 등록
    py tools/mark_event.py add "국제대회 3종" --dry-run       등록하지 않고 이력과의 유사도만 보기
    py tools/mark_event.py add "..." --file data/processed/collected_20260928_100808.csv
    py tools/mark_event.py remove 6                           6번 이력 삭제 (번호는 list 기준)

왜 필요한가
----------
사건 이력은 초안을 저장할 때 collect 가 자동으로 남긴다. 그런데 두 경우에
이력이 비어 같은 사건이 다시 선정된다.

  - 이력 기능 이전에 쓴 글 (2026-09-23 15:02 이전). 조직 레인 조회일수가
    14일이라 그 사건의 다른 매체 기사가 10/7 까지 후보로 올라온다.
    실측(2026-09-28 dry-run): 9/23 오전 글의 '국제대회 3종 춘천' 이 조직 3위.
  - 사람이 Notion 에서 직접 쓴 글, 다른 경로로 이미 다룬 소재.

dry-run 에서 이런 기사를 발견하면 제목으로 찾아 등록한다. 다음 실행부터
'이전 글에서 다룬 사건, 후보 제외' 로 걸러진다.

잘못 저장된 이력을 지울 때도 쓴다. 예) 본문 정제가 KBS 하단 문구를 못 걷어내
시그니처 절반이 '구독해주세요·카카오톡…' 인 이력 — 같은 하단을 가진 KBS 기사가
주제와 무관하게 전부 '같은 사건' 으로 걸린다.

파이프라인과 같은 계산을 쓴다
---------------------------
시그니처는 ranking_agent.build_signatures() 로, 판정은 same_event() 로 한다.
문서 빈도의 기준 집합은 수집 CSV 전체다. 실제 실행은 처리 이력을 뺀 후보로
계산하므로 조금 다르지만, 빈도 상한(전체의 50%)에 걸리는 일반어만 달라져
결과는 거의 같다.

등록한 이력은 collect 가 남긴 것과 같은 규칙으로 늙는다 — 등록 시각부터
keep_days(가장 긴 레인 조회일수)가 지나면 다음 저장 때 지워진다.

⚠️ state/state.json 을 직접 고친다. 로컬 상태는 Actions 가 커밋한 최신
   상태보다 뒤처져 있기 쉽다. 반드시 `git pull` 뒤에 쓰고, 쓴 뒤에는 바로
   커밋·푸시한다. 그러지 않으면 다음 Actions 실행이 이 변경을 모르고 돌거나,
   푸시할 때 Actions 의 상태 갱신을 덮어쓴다.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.collecting.body_cleaner import clean_all                 # noqa: E402
from agents.ranking.ranking_agent import (                           # noqa: E402
    EVENT_OVERLAP,
    EVENT_OVERLAP_MIN_SHARED,
    MAX_SIMILARITY,
    build_signatures,
    jaccard,
    same_event,
)
from config.settings import KST, PROCESSED_DIR                       # noqa: E402
from core import state_store as state                                # noqa: E402
from core.article_models import Article                              # noqa: E402

PREVIEW_TOKENS = 12   # list 에서 제목이 없는 이력을 보여 줄 토큰 수


# ── CSV ─────────────────────────────────────────────────
def _latest_csv() -> Path | None:
    files = sorted(PROCESSED_DIR.glob("collected_*.csv"))
    return files[-1] if files else None


def _load(path: Path) -> list[Article]:
    """CSV 한 줄을 Article 로 되돌린다. sweep.py 와 같은 방식."""
    fields = {f.name for f in Article.__dataclass_fields__.values()}
    articles = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            data = {k: v for k, v in row.items() if k in fields}
            data["search_rank"] = int(data.get("search_rank") or 0)
            data["subtopics"] = [
                s for s in (data.get("subtopics") or "").split("\n") if s.strip()
            ]
            for drop in ("keyword_score", "score_norm", "report_count", "matched_keywords"):
                data.pop(drop, None)
            articles.append(Article(**data))
    return articles


# ── 표시 ─────────────────────────────────────────────────
def _label(e: dict) -> str:
    """이력 한 줄의 이름. 수동 등록분은 제목, 자동 저장분은 토큰 앞부분."""
    if e.get("title"):
        return e["title"]
    tokens = e["tokens"].split()
    return " ".join(tokens[:PREVIEW_TOKENS]) + (" …" if len(tokens) > PREVIEW_TOKENS else "")


def _compare(sig: set[str], other: set[str]) -> tuple[float, float, int]:
    """(Jaccard, 겹침 계수, 공유 토큰 수)."""
    shared = len(sig & other)
    small = min(len(sig), len(other)) or 1
    return jaccard(sig, other), shared / small, shared


# ── 명령 ─────────────────────────────────────────────────
def cmd_list(st: dict) -> None:
    events = st.get("written_events", [])
    if not events:
        print("사건 이력이 비어 있습니다.")
        return
    print(f"사건 이력 {len(events)}건 (state 갱신 기준: {st.get('last_collected_at')})\n")
    for i, e in enumerate(events, 1):
        kind = "수동" if e.get("by") == "manual" else "자동"
        n = len(e["tokens"].split())
        print(f"  [{i:>2}] {e['at'][:16]}  {kind}  {n:>3}토큰  {_label(e)[:70]}")


def cmd_add(st: dict, args: argparse.Namespace) -> None:
    path = Path(args.file) if args.file else _latest_csv()
    if not path or not path.exists():
        print(f"수집 CSV 를 찾을 수 없습니다: {path or PROCESSED_DIR}")
        raise SystemExit(1)

    # 정제된 전체 = 문서 빈도 기준 집합. 파이프라인이 정제 후에 시그니처를 만든다.
    pool = clean_all(_load(path))
    query = args.query.replace(" ", "")
    hits = [a for a in pool if query in a.title.replace(" ", "")]
    print(f"CSV: {path.name} · 정제 후 {len(pool)}건 · '{args.query}' 검색 {len(hits)}건\n")
    if not hits:
        print("제목이 일치하는 기사가 없습니다. 다른 CSV 는 --file 로 지정하세요.")
        raise SystemExit(1)

    for i, a in enumerate(hits, 1):
        print(f"  [{i}] {a.published[:10]}  {a.press or a.source:<12}  {a.title[:60]}")
    print()

    if args.all:
        chosen = hits
    elif args.pick:
        bad = [n for n in args.pick if not 1 <= n <= len(hits)]
        if bad:
            print(f"없는 번호: {bad}")
            raise SystemExit(1)
        chosen = [hits[n - 1] for n in args.pick]
    elif len(hits) == 1:
        chosen = hits
    else:
        print("여러 건이 찾아졌습니다. --pick 번호 또는 --all 로 고르세요.")
        raise SystemExit(1)

    signatures = build_signatures(pool)
    history = [
        (i, set(e["tokens"].split()), e)
        for i, e in enumerate(st.get("written_events", []), 1)
        if e["tokens"]
    ]
    now = datetime.now(KST).isoformat(timespec="seconds")
    new: list[dict] = []

    for a in chosen:
        sig = signatures.get(a.url, set())
        print(f"▶ {a.title[:60]}  ({len(sig)}토큰)")
        if not sig:
            print("   시그니처가 비었습니다 — 건너뜁니다\n")
            continue

        # 이미 이력과 같은 사건으로 판정되면 등록할 필요가 없다.
        # 가장 가까운 이력을 함께 보여 줘 '왜 안 걸렸는지' 를 볼 수 있게 한다.
        scored = sorted(
            ((i, *_compare(sig, s), same_event(sig, s), e) for i, s, e in history),
            key=lambda x: (x[4], x[1], x[2]),
            reverse=True,
        )
        for i, j, o, shared, hit, e in scored[:3]:
            mark = "같은 사건" if hit else "다름"
            print(
                f"   이력 [{i:>2}] J {j:.3f} · O {o:.2f} · 공유 {shared:>3}  → {mark}"
                f"  | {_label(e)[:40]}"
            )
        if any(x[4] for x in scored) and not args.force:
            print("   이미 이력으로 걸립니다 — 건너뜁니다 (--force 로 등록 가능)\n")
            continue
        print()
        new.append({
            "at": now,
            "tokens": " ".join(sorted(sig)),
            "title": a.title,
            "by": "manual",
        })

    print(
        f"판정 기준: Jaccard ≥ {MAX_SIMILARITY} 또는 "
        f"(공유 ≥ {EVENT_OVERLAP_MIN_SHARED} · 겹침 계수 ≥ {EVENT_OVERLAP})"
    )
    if not new:
        print("등록할 이력이 없습니다.")
        return
    if args.dry_run:
        print(f"[dry-run] {len(new)}건을 등록하지 않았습니다.")
        return

    st["written_events"] = st.get("written_events", []) + new
    state.save(st)
    print(f"{len(new)}건 등록 (이력 {len(st['written_events'])}건). 커밋·푸시를 잊지 마세요.")


def cmd_remove(st: dict, args: argparse.Namespace) -> None:
    events = st.get("written_events", [])
    bad = [n for n in args.index if not 1 <= n <= len(events)]
    if bad:
        print(f"없는 번호: {bad} (현재 {len(events)}건 — list 로 확인)")
        raise SystemExit(1)

    targets = sorted(set(args.index))
    for n in targets:
        print(f"  삭제 [{n:>2}] {events[n - 1]['at'][:16]}  {_label(events[n - 1])[:70]}")
    if not args.yes and input("\n삭제할까요? [y/N] ").strip().lower() != "y":
        print("취소했습니다.")
        return

    drop = {n - 1 for n in targets}
    st["written_events"] = [e for i, e in enumerate(events) if i not in drop]
    state.save(st)
    print(f"{len(targets)}건 삭제 (이력 {len(st['written_events'])}건). 커밋·푸시를 잊지 마세요.")


def main() -> None:
    p = argparse.ArgumentParser(description="사건 이력 수동 관리")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="사건 이력 보기")

    a = sub.add_parser("add", help="수집 CSV 에서 제목으로 찾아 이력에 등록")
    a.add_argument("query", help="제목 일부 (공백 무시)")
    a.add_argument("--file", help="collected_*.csv 경로 (기본: 가장 최근 파일)")
    a.add_argument("--pick", type=int, nargs="+", help="검색 결과 중 등록할 번호")
    a.add_argument("--all", action="store_true", help="검색 결과 전부 등록")
    a.add_argument("--force", action="store_true", help="이미 걸려도 등록")
    a.add_argument("--dry-run", action="store_true", help="유사도만 보고 저장하지 않음")

    r = sub.add_parser("remove", help="이력 삭제 (번호는 list 기준)")
    r.add_argument("index", type=int, nargs="+")
    r.add_argument("--yes", action="store_true", help="확인 없이 삭제")

    args = p.parse_args()

    # 정제·상태 모듈이 찍는 로그는 표를 가린다. 경고 이상만 남긴다.
    logging.getLogger().setLevel(logging.WARNING)

    st = state.load()
    if args.cmd == "list":
        cmd_list(st)
    elif args.cmd == "add":
        cmd_add(st, args)
    else:
        cmd_remove(st, args)


if __name__ == "__main__":
    main()
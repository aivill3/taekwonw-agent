"""사건 시그니처 유사도 측정 — 같은 사건이 왜 안 묶였는지 실데이터로 본다.

읽기 전용 오프라인 도구다. 파이프라인과 같은 build_signatures() 를 쓴다.

    uv run python -m tools.event_pairs --file data/processed/collected_20260922_235453.csv --grep "태권도대회 3종"
    uv run python -m tools.event_pairs --file ... --grep 폐막 마무리
    uv run python -m tools.event_pairs --file ... --near            # 놓친 쌍 후보
    uv run python -m tools.event_pairs --file ... --with 용인시의회  # 이 기사와 묶이는 기사 전부

왜 필요한가 (2026-09-23)
-----------------------
같은 사건인데 묶이지 않은 쌍이 두 번 나왔다.
  조직 레인  '국제 태권도대회 3종 2029년까지 (강원) 춘천서 열린다' 두 기사가
            한 글에 두 챕터로 들어갔다
  레인 간    '춘천 세계태권도품새선수권 폐막' 과 '…품새선수권대회 마무리' 가
            대회·태권도 레인에 따로 들어갔다

의심하는 원인은 Jaccard(교집합/합집합)다. 짧은 기사와 긴 기사를 비교하면
합집합이 긴 쪽 토큰으로 채워져, 짧은 쪽 토큰이 거의 다 겹쳐도 값이 낮다.
겹침 계수(교집합/작은 쪽)는 길이 차이에 흔들리지 않는다. 지표를 바꾸기 전에
두 값을 실제 쌍에서 나란히 본다.

출력 열
------
  J      Jaccard (EVENT_SIMILARITY 와 비교)
  O      겹침 계수 = 공유 / min(두 시그니처 크기)
  공유   공유 토큰 수
  크기   두 기사의 시그니처 토큰 수
  [묶임]  파이프라인 판정(same_event) 결과. J 또는 O 기준 중 하나를 넘으면 묶인다
"""
import argparse
import itertools
from pathlib import Path

from agents.collecting.body_cleaner import clean_all
from agents.ranking.ranking_agent import (
    EVENT_OVERLAP,
    EVENT_OVERLAP_MIN_SHARED,
    MAX_SIMILARITY,
    build_signatures,
    jaccard,
    same_event,
)
from tools.sweep import _latest_csv, _load

# 파이프라인과 같은 값 (.env · 환경변수 EVENT_SIMILARITY 를 따른다)
THRESHOLD = MAX_SIMILARITY


def overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _row(a, b, sigs) -> tuple:
    sa, sb = sigs.get(a.url, set()), sigs.get(b.url, set())
    return jaccard(sa, sb), overlap(sa, sb), len(sa & sb), len(sa), len(sb), sa & sb


def _print(a, b, row, sigs, show_shared: bool) -> None:
    j, o, shared, na, nb, common = row
    sa, sb = sigs.get(a.url, set()), sigs.get(b.url, set())
    mark = "묶임" if same_event(sa, sb) else "안 묶임"
    print(f"J {j:.3f}  O {o:.3f}  공유 {shared:3d}  크기 {na}/{nb}  [{mark}]")
    print(f"   A {a.title[:60]}  ({len(a.body_clean)}자)")
    print(f"   B {b.title[:60]}  ({len(b.body_clean)}자)")
    if show_shared:
        print(f"   공유 토큰: {' '.join(sorted(common)[:30])}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, help="collected_*.csv (기본: 가장 최근)")
    ap.add_argument("--grep", nargs="+", help="제목에 이 문자열 중 하나라도 있는 기사끼리 비교")
    ap.add_argument("--near", action="store_true",
                    help="Jaccard 는 기준 미달인데 겹침 계수가 높은 쌍 (놓친 후보)")
    ap.add_argument("--with", dest="with_", nargs="+",
                    help="제목에 이 문자열이 있는 기사와 같은 사건으로 판정되는 기사 전부 (과잉 묶음 점검)")
    ap.add_argument("--min-overlap", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    path = args.file or _latest_csv()
    if not path:
        raise SystemExit("data/processed/collected_*.csv 가 없습니다")
    articles = clean_all(_load(path))
    # 파이프라인은 세 레인 후보를 한데 모아 시그니처를 만든다. CSV 가 곧 그 집합이다.
    sigs = build_signatures(articles)
    print(
        f"{path.name} · 기사 {len(articles)}건 · EVENT_SIMILARITY {THRESHOLD} "
        f"· EVENT_OVERLAP {EVENT_OVERLAP}/{EVENT_OVERLAP_MIN_SHARED}\n"
    )

    if args.grep:
        picked = [a for a in articles if any(g in a.title for g in args.grep)]
        print(f"제목 일치 {len(picked)}건\n")
        rows = [(a, b, _row(a, b, sigs)) for a, b in itertools.combinations(picked, 2)]
        rows.sort(key=lambda r: r[2][1], reverse=True)
        for a, b, row in rows[: args.limit]:
            _print(a, b, row, sigs, show_shared=True)
        return

    if args.with_:
        seeds = [a for a in articles if any(g in a.title for g in args.with_)]
        for seed in seeds:
            ss = sigs.get(seed.url, set())
            hits = []
            for b in articles:
                if b is seed:
                    continue
                sb = sigs.get(b.url, set())
                if same_event(ss, sb):
                    j, o = jaccard(ss, sb), overlap(ss, sb)
                    by = "J" if j >= THRESHOLD else "O"
                    hits.append((j, o, len(ss & sb), by, b))
            hits.sort(key=lambda h: h[0], reverse=True)
            print(f"■ {seed.title[:60]}  ({len(seed.body_clean)}자, 시그니처 {len(ss)})")
            print(f"  같은 사건 판정 {len(hits)}건")
            for j, o, shared, by, b in hits[: args.limit]:
                print(f"    J {j:.3f}  O {o:.3f}  공유 {shared:3d}  [{by}]  {b.title[:55]}")
            print()
        return

    if args.near:
        rows = []
        for a, b in itertools.combinations(articles, 2):
            row = _row(a, b, sigs)
            if row[0] < THRESHOLD and row[1] >= args.min_overlap:
                rows.append((a, b, row))
        rows.sort(key=lambda r: r[2][1], reverse=True)
        print(f"Jaccard < {THRESHOLD} 이면서 겹침 계수 ≥ {args.min_overlap}: {len(rows)}쌍\n")
        for a, b, row in rows[: args.limit]:
            _print(a, b, row, sigs, show_shared=False)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
#!/usr/bin/env python
"""임계값 조합 스윕 — 저장된 수집 CSV 로 선정 결과를 한 번에 비교한다.

    py tools/sweep.py                                  기본 조합 (우선순위 × 유사도)
    py tools/sweep.py --priority 0 1 3 --similarity 0.10 0.12 0.15
    py tools/sweep.py --core 2 3 4
    py tools/sweep.py --file data/processed/collected_20260918_130032.csv
    py tools/sweep.py --append                         한 파일에 날짜별로 쌓기

왜 필요한가
----------
dry-run 한 번은 90초인데, 그중 판단 로직은 1초도 안 된다. 나머지는 네이버
200건 수집과 본문 추출 90건이고, 임계값을 바꿔도 그 결과는 똑같다. 저장된
CSV 에서 시작하면 조합 하나에 1초도 안 걸린다.

무엇을 보는가
-----------
조합마다 레인별 선정 4건과 '같은 소재 겹침'을 찍는다. 겹침은 선정된 기사들의
사건 시그니처를 서로 비교한 값이다. 한 글은 챕터 넷이고 챕터마다 사건 하나가
들어가므로, 선정 4건이 서로 닮았다면 같은 행사를 네 번 쓰는 글이 된다.
EVENT_SIMILARITY 를 고르는 실제 기준이 이 값이다.

파이프라인과 같은 코드를 쓴다
-------------------------
collect_workflow.select_by_lane() 을 그대로 부른다. 레인 분리, 레인 간 사건
제외, 주제 적합성, 사건 묶기가 전부 실제 실행과 동일하다.

한 가지만 다르다: 수집 CSV 는 URL 당 한 줄이라, 두 레인에 모두 걸렸던 기사가
한 레인에만 남는다. 실제 실행에서도 그런 기사는 레인 간 제외로 앞 레인이
가져가므로 결과 차이는 거의 없지만, 뒤 레인 후보 수가 조금 적게 나온다.
"""
import argparse
import csv
import itertools
import logging
import os
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.collecting.body_cleaner import clean_all           # noqa: E402
from config.settings import KST, PROCESSED_DIR, QUALITY_DIR    # noqa: E402
from core.article_models import Article                        # noqa: E402

# 조합을 안 주면 쓰는 기본값. 지금 열려 있는 결정 두 가지다.
DEFAULT_PRIORITY = ["0", "1", "2", "3"]
DEFAULT_SIMILARITY = ["0.10", "0.12", "0.15"]

# 스윕이 돌리는 손잡이. (인자 이름, 환경변수 이름)
KNOBS = [
    ("priority", "CLUSTER_PRIORITY_MIN"),
    ("similarity", "EVENT_SIMILARITY"),
    ("core", "MIN_CORE_HITS"),
    ("mentions", "MIN_CORE_MENTIONS"),
    ("body", "MIN_BODY_CHARS"),
]


def _latest_csv() -> Path | None:
    files = sorted(PROCESSED_DIR.glob("collected_*.csv"))
    return files[-1] if files else None


def _load(path: Path) -> list[Article]:
    """CSV 한 줄을 Article 로 되돌린다. check_ranking.py 와 같은 방식."""
    fields = {f.name for f in Article.__dataclass_fields__.values()}
    articles = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            data = {k: v for k, v in row.items() if k in fields}
            data["search_rank"] = int(data.get("search_rank") or 0)
            data["subtopics"] = [
                s for s in (data.get("subtopics") or "").split("\n") if s.strip()
            ]
            for drop in (
                "keyword_score", "score_norm", "report_count", "matched_keywords"
            ):
                data.pop(drop, None)
            articles.append(Article(**data))
    return articles


def _reload_with(env: dict[str, str]):
    """환경변수를 세팅하고 판단 모듈을 다시 읽는다.

    임계값은 전부 모듈을 읽을 때 os.getenv 로 한 번만 잡힌다. 그래서 값을
    바꾸려면 다시 읽어야 한다. ranking_agent 를 먼저 읽어야 collect_workflow
    가 새 함수·새 상수를 집는다 (from ... import 로 값을 복사해 가기 때문).
    """
    import importlib
    os.environ.update(env)
    import agents.ranking.ranking_agent as R
    importlib.reload(R)
    import workflows.collect_workflow as W
    importlib.reload(W)
    return R, W


def _overlap(selected: list[Article], R) -> tuple[float, int]:
    """선정분끼리 얼마나 닮았는지. (최대 유사도, 0.05 넘는 쌍 수)

    한 글 안에서 같은 행사가 여러 챕터로 반복되는지를 재는 값이다.
    """
    if len(selected) < 2:
        return 0.0, 0
    sigs = R.build_signatures(selected)
    pairs = [
        R.jaccard(sigs.get(a.url, set()), sigs.get(b.url, set()))
        for a, b in itertools.combinations(selected, 2)
    ]
    return max(pairs), sum(1 for p in pairs if p >= 0.05)


def main() -> None:
    p = argparse.ArgumentParser(description="임계값 조합 스윕")
    p.add_argument("--file", help="collected_*.csv 경로 (기본: 가장 최근 파일)")
    p.add_argument("--priority", nargs="+", help="CLUSTER_PRIORITY_MIN 후보")
    p.add_argument("--similarity", nargs="+", help="EVENT_SIMILARITY 후보")
    p.add_argument("--core", nargs="+", help="MIN_CORE_HITS 후보")
    p.add_argument("--mentions", nargs="+", help="MIN_CORE_MENTIONS 후보")
    p.add_argument("--body", nargs="+", help="MIN_BODY_CHARS 후보")
    p.add_argument("--out", help="결과 CSV 경로 (기본: data/quality/sweep_*.csv)")
    p.add_argument("--append", action="store_true", help="기존 파일에 이어 쓴다")
    p.add_argument("--verbose", action="store_true", help="파이프라인 로그도 보인다")
    args = p.parse_args()

    # 조합마다 파이프라인이 수십 줄씩 찍는다. 표가 묻히므로 기본은 잠근다.
    logging.getLogger().setLevel(logging.INFO if args.verbose else logging.ERROR)

    path = Path(args.file) if args.file else _latest_csv()
    if not path or not path.exists():
        print(f"CSV 를 찾을 수 없습니다: {path or PROCESSED_DIR}")
        raise SystemExit(1)

    # 정제는 임계값과 무관하므로 한 번만 한다.
    articles = clean_all(_load(path))
    if not articles:
        print("본문이 있는 기사가 없습니다.")
        raise SystemExit(1)

    # 조합 만들기. 값을 안 준 손잡이는 현재 설정 그대로 둔다(= 후보 1개).
    given = {
        "priority": args.priority or DEFAULT_PRIORITY,
        "similarity": args.similarity or DEFAULT_SIMILARITY,
        "core": args.core,
        "mentions": args.mentions,
        "body": args.body,
    }
    names = [n for n, _ in KNOBS]
    axes = [given[n] if given[n] else [None] for n in names]
    combos = list(itertools.product(*axes))

    print(f"\n파일: {path.name} · 기사 {len(articles)}건 · 조합 {len(combos)}개\n")
    header = f"{'우선':>4} {'유사도':>6}  {'레인':<6} {'후보':>4} {'겹침':>5}  선정"
    print(header)
    print("-" * 110)

    rows = []
    run_at = datetime.now(KST).replace(microsecond=0).isoformat()

    for combo in combos:
        env = {
            var: str(val)
            for (name, var), val in zip(KNOBS, combo)
            if val is not None
        }
        R, W = _reload_with(env)

        lanes = W._split_by_lane(articles)
        per_post = W.BUNDLE_SIZE * W.POSTS_PER_KEYWORD
        picks, _ = W.select_by_lane(lanes, per_post)

        for lane_name, (selected, _clusters) in picks.items():
            worst, close = _overlap(selected, R)
            picks_text = " · ".join(
                f"{a.title[:16]}({a.report_count})" for a in selected
            )
            print(
                f"{R.CLUSTER_PRIORITY_MIN:>4} {R.MAX_SIMILARITY:>6.2f}  "
                f"{lane_name:<6} {len(lanes.get(lane_name, [])):>4} "
                f"{worst:>5.2f}  {picks_text}"
            )
            rows.append({
                "run_at": run_at,
                "source_csv": path.name,
                "cluster_priority": R.CLUSTER_PRIORITY_MIN,
                "event_similarity": R.MAX_SIMILARITY,
                "min_core_hits": R.MIN_CORE_HITS,
                "min_core_mentions": R.MIN_CORE_MENTIONS,
                "min_body_chars": R.MIN_BODY_CHARS,
                "lane": lane_name,
                "candidates": len(lanes.get(lane_name, [])),
                "selected": len(selected),
                "max_pair_similarity": round(worst, 3),
                "close_pairs": close,
                "report_counts": "|".join(str(a.report_count) for a in selected),
                "titles": "|".join(a.title for a in selected),
            })
        print()

    QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    if args.out:
        out = Path(args.out)
    elif args.append:
        out = QUALITY_DIR / "sweep.csv"
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = QUALITY_DIR / f"sweep_{stamp}.csv"

    exists = out.exists()
    mode = "a" if args.append and exists else "w"
    with out.open(mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if mode == "w" or not exists:
            writer.writeheader()
        writer.writerows(rows)

    print(f"결과 {len(rows)}줄 저장: {out}")
    print(
        "\n읽는 법\n"
        "  겹침  선정 4건끼리의 최대 사건 유사도. 높을수록 한 글에 같은 행사가\n"
        "        여러 챕터로 들어간다. 낮은 조합을 고른다.\n"
        "  (n)   그 기사 사건을 보도한 매체 수.\n"
    )


if __name__ == "__main__":
    main()
#!/usr/bin/env python
"""주제 적합성 필터 진단 — 수집 CSV 로 임계값을 맞춰 본다.

    py tools/check_ranking.py                     가장 최근 collected_*.csv
    py tools/check_ranking.py --keyword 태권도조직   그 키워드 후보만
    py tools/check_ranking.py --core 2            고유어 기준을 2로 가정
    py tools/check_ranking.py --core 2 --hits 4   두 기준을 함께 가정
    py tools/check_ranking.py --file data/processed/collected_20260918_130032.csv

예전에는 Notion 뉴스 DB 의 '선정됨' 카드를 읽었다. 뉴스 DB 를 쓰지 않게
되면서(2026-09) collect 가 남기는 CSV 를 보도록 바꿨다.

왜 필요한가
----------
키워드를 늘리면 통과율이 키워드마다 달라진다. 대회·선수 기사는 고유어가
넉넉하지만, 협회·조직 기사는 짧고 고유어가 2개 안팎이라 같은 기준에서
무더기로 탈락한다. 어떤 기사가 어느 관문에서 걸렸는지 숫자로 봐야
임계값을 옮길지 판단할 수 있다.

--core / --hits 는 이 실행에만 적용된다. 값을 정한 뒤에는 .env 나
collect.yml 에 MIN_CORE_HITS / MIN_DOMAIN_HITS 로 넣는다.

CSV 의 본문은 정제 전 상태다. 이 도구가 body_cleaner 를 그대로 돌려
파이프라인과 같은 조건으로 판정한다.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.collecting.body_cleaner import clean_all           # noqa: E402
from agents.ranking import ranking_agent as R                  # noqa: E402
from config.settings import PROCESSED_DIR                      # noqa: E402
from core.article_models import Article                        # noqa: E402
from core.logger import setup                                  # noqa: E402


def _latest_csv() -> Path | None:
    files = sorted(PROCESSED_DIR.glob("collected_*.csv"))
    return files[-1] if files else None


def _load(path: Path) -> list[Article]:
    """CSV 한 줄을 Article 로 되돌린다. 없는 열은 기본값으로 둔다."""
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


def main() -> None:
    p = argparse.ArgumentParser(description="주제 적합성 필터 진단")
    p.add_argument("--file", help="collected_*.csv 경로 (기본: 가장 최근 파일)")
    p.add_argument("--keyword", help="이 검색 키워드의 기사만 본다")
    p.add_argument("--core", type=int, help=f"고유어 기준 (현재 {R.MIN_CORE_HITS})")
    p.add_argument("--hits", type=int, help=f"도메인 키워드 기준·제목O (현재 {R.MIN_DOMAIN_HITS})")
    p.add_argument(
        "--hits-no-title", type=int,
        help=f"도메인 키워드 기준·제목X (현재 {R.MIN_DOMAIN_HITS_NO_TITLE})",
    )
    p.add_argument(
        "--mentions",
        type=int,
        default=None,
        help=f"핵심어 언급 횟수 기준 (현재 {R.MIN_CORE_MENTIONS})",
    )
    p.add_argument(
        "--no-crime", action="store_true", help="사건 기사 제외 규칙을 끄고 본다"
    )
    p.add_argument(
        "--dispute", action="store_true", help="협회 징계·소송 기사도 제외해 본다"
    )
    args = p.parse_args()

    setup()
    path = Path(args.file) if args.file else _latest_csv()
    if not path or not path.exists():
        print(f"CSV 를 찾을 수 없습니다: {path or PROCESSED_DIR}")
        raise SystemExit(1)

    # 가정값 적용. 모듈 상수를 바꾸면 is_on_topic 이 그대로 읽는다.
    before = (R.MIN_CORE_HITS, R.MIN_DOMAIN_HITS, R.MIN_DOMAIN_HITS_NO_TITLE)
    if args.core is not None:
        R.MIN_CORE_HITS = args.core
    if args.hits is not None:
        R.MIN_DOMAIN_HITS = args.hits
    if args.hits_no_title is not None:
        R.MIN_DOMAIN_HITS_NO_TITLE = args.hits_no_title
    if args.mentions is not None:
        R.MIN_CORE_MENTIONS = args.mentions
    after = (R.MIN_CORE_HITS, R.MIN_DOMAIN_HITS, R.MIN_DOMAIN_HITS_NO_TITLE)

    # 사건 기사 제외도 같은 방식으로 가정해 본다.
    if args.no_crime:
        R.EXCLUDE_CRIME = False
    if args.dispute:
        R.EXCLUDE_DISPUTE = True
    incident = (
        "끔"
        if not R.EXCLUDE_CRIME
        else ("형사+분쟁" if R.EXCLUDE_DISPUTE else "형사만")
    )

    articles = clean_all(_load(path))
    if args.keyword:
        articles = [a for a in articles if a.search_keyword == args.keyword]
    if not articles:
        print("대상 기사가 없습니다.")
        raise SystemExit(1)

    print(f"\n파일: {path.name} · 기사 {len(articles)}건")
    print(f"기준: 고유어 {after[0]} · 도메인 제목O {after[1]} / 제목X {after[2]}", end="")
    print(f"   (기본값 {before[0]} / {before[1]} / {before[2]})")
    print(
        f"언급 {R.MIN_CORE_MENTIONS}회 이상이면 종류 수 미달도 통과 · "
        f"본문 {R.MIN_BODY_CHARS}자 미만 제외"
    )
    print(f"사건 기사 제외: {incident} (본문 기준 {R.CRIME_BODY_HITS}개)\n")

    passed: dict[str, int] = {}
    total: dict[str, int] = {}
    for a in sorted(articles, key=lambda x: (x.search_keyword, x.search_rank)):
        ok, reason = R.is_on_topic(a)
        core = R.count_core_hits(a)
        mentions = R.count_core_mentions(a)
        hits = R.count_domain_hits(a)
        chars = len(a.body_clean)
        density = core / (chars / 1000) if chars else 0.0
        kw = a.search_keyword or "-"
        total[kw] = total.get(kw, 0) + 1
        passed[kw] = passed.get(kw, 0) + (1 if ok else 0)
        mark = "O" if ok else "X"
        title = "제목O" if R.has_domain_in_title(a) else "제목X"
        print(
            f"[{mark}] {kw} {a.search_rank:>2}위 | 고유어 {core:>2} · 언급 {mentions:>3} · "
            f"도메인 {hits:>2} · 밀도 {density:>5.2f} · {chars:>5,}자 · {title} | {a.title[:38]}"
        )
        if not ok:
            print(f"      탈락: {reason}")

    print("\n키워드별 통과")
    for kw in total:
        print(f"  {kw}: {passed[kw]}/{total[kw]}건")
    print(
        "\n값을 정했으면 .env 나 collect.yml 에 MIN_CORE_HITS / MIN_DOMAIN_HITS /"
        " MIN_DOMAIN_HITS_NO_TITLE 로 넣으세요."
    )


if __name__ == "__main__":
    main()
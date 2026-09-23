#!/usr/bin/env python
"""고유어 카운트 방식 교체 전후 비교 — 임계값을 다시 맞추기 위한 일회용 도구.

    py tools/compare_counting.py                    가장 최근 collected_*.csv
    py tools/compare_counting.py --keyword 태권도     그 키워드 후보만
    py tools/compare_counting.py --file data/processed/collected_20260921_105235.csv

왜 필요한가
----------
2026-09-21 에 주제 적합성 판정을 '토큰 종류 수'에서 '사전 단어 직접 검색'으로
바꿨다. 척도가 달라지므로 MIN_CORE_HITS / MIN_DOMAIN_HITS 를 그대로 쓸 수 없다.

    구 방식: tokenize() 로 자른 토큰 중 사전어를 포함하는 토큰의 종류 수
             → 대회 공식명이 길수록 토큰이 뭉쳐 값이 작아진다
    신 방식: 원문에 실제로 등장하는 사전 단어의 종류 수
             → 토큰 경계와 무관하지만, 사전 크기(CORE 27개)에 상한이 묶인다

이 도구는 같은 CSV 에 두 방식을 모두 적용해 기사별 값과 판정 변화를 보여준다.
임계값을 확정한 뒤에는 지워도 된다.

읽는 법
------
  '판정' 열의 ↑ 는 탈락→통과, ↓ 는 통과→탈락이다.
  맨 아래 분포표에서 '통과 기사의 최솟값'과 '탈락 기사의 최댓값' 사이가
  비어 있으면 그 사이 값이 안전한 임계값이다.
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

# 구 방식이 쓰던 사전. '띠' 는 신 방식에서 뺐으므로 여기서만 되살린다.
LEGACY_CORE = (R.CORE_KEYWORDS - {"검은띠"}) | {"띠"}
LEGACY_DOMAIN = LEGACY_CORE | R.GENERIC_KEYWORDS


def _legacy_core_hits(article: Article) -> int:
    tokens = set(R.tokenize(f"{article.title}\n{article.body_clean}"))
    return sum(1 for tk in tokens if any(d in tk for d in LEGACY_CORE))


def _legacy_domain_hits(article: Article) -> int:
    tokens = set(R.tokenize(f"{article.title}\n{article.body_clean}"))
    return sum(1 for tk in tokens if any(d in tk for d in LEGACY_DOMAIN))


def _legacy_in_title(article: Article) -> bool:
    return any(
        any(d in tk for d in LEGACY_CORE) for tk in R.tokenize(article.title)
    )


def _verdict(article: Article, legacy: bool) -> tuple[bool, str]:
    """is_on_topic() 을 구/신 카운트로 각각 돌린다.

    모듈 함수를 잠깐 바꿔 끼운다. is_on_topic() 안의 호출부를 건드리지 않고
    같은 관문 구성 그대로 비교하기 위해서다.
    """
    if not legacy:
        return R.is_on_topic(article)
    saved = (R.count_core_hits, R.count_domain_hits, R.has_domain_in_title)
    R.count_core_hits = _legacy_core_hits
    R.count_domain_hits = _legacy_domain_hits
    R.has_domain_in_title = _legacy_in_title
    try:
        return R.is_on_topic(article)
    finally:
        R.count_core_hits, R.count_domain_hits, R.has_domain_in_title = saved


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


def _spread(label: str, values: list[int], passed: list[bool]) -> None:
    """통과/탈락 두 집단의 값 분포를 찍는다. 임계값을 어디에 둘지 보기 위한 것."""
    yes = sorted(v for v, p in zip(values, passed) if p)
    no = sorted(v for v, p in zip(values, passed) if not p)
    y = f"{yes[0]} ~ {yes[-1]}" if yes else "-"
    n = f"{no[0]} ~ {no[-1]}" if no else "-"
    gap = ""
    if yes and no and min(yes) > max(no):
        gap = f"  → {max(no) + 1} ~ {min(yes)} 사이가 비어 있음"
    print(f"  {label:<18} 통과 {y:<12} 탈락 {n:<12}{gap}")


def main() -> None:
    p = argparse.ArgumentParser(description="고유어 카운트 방식 구/신 비교")
    p.add_argument("--file", help="collected_*.csv 경로 (기본: 가장 최근 파일)")
    p.add_argument("--keyword", help="이 검색 키워드의 기사만 본다")
    args = p.parse_args()

    setup()
    path = Path(args.file) if args.file else _latest_csv()
    if not path or not path.exists():
        print(f"CSV 를 찾을 수 없습니다: {path or PROCESSED_DIR}")
        raise SystemExit(1)

    articles = clean_all(_load(path))
    if args.keyword:
        articles = [a for a in articles if a.search_keyword == args.keyword]
    if not articles:
        print("대상 기사가 없습니다.")
        raise SystemExit(1)

    print(f"\n파일: {path.name} · 기사 {len(articles)}건")
    print(
        f"기준: 고유어 {R.MIN_CORE_HITS}종 · 언급 {R.MIN_CORE_MENTIONS}회 · "
        f"도메인 제목O {R.MIN_DOMAIN_HITS} / 제목X {R.MIN_DOMAIN_HITS_NO_TITLE}\n"
    )
    print(f"{'순위':<12} {'고유어':<10} {'도메인':<10} {'판정':<6} 제목")
    print("-" * 96)

    new_core: list[int] = []
    new_domain: list[int] = []
    new_pass: list[bool] = []
    moved: list[str] = []

    for a in sorted(articles, key=lambda x: (x.search_keyword, x.search_rank)):
        oc, nc = _legacy_core_hits(a), R.count_core_hits(a)
        od, nd = _legacy_domain_hits(a), R.count_domain_hits(a)
        ook, _ = _verdict(a, legacy=True)
        nok, why = _verdict(a, legacy=False)

        mark = "  " if ook == nok else ("↑" if nok else "↓")
        state = ("O" if ook else "X") + "→" + ("O" if nok else "X")
        pos = f"{a.search_keyword} {a.search_rank}위"
        print(
            f"{pos:<12} {oc:>3}→{nc:<5} {od:>3}→{nd:<5} "
            f"{mark}{state:<5} {a.title[:40]}"
        )
        if ook != nok:
            moved.append(f"  {mark} [{pos}] {a.title[:44]}\n       {why}")

        new_core.append(nc)
        new_domain.append(nd)
        new_pass.append(nok)

    print()
    if moved:
        print(f"판정이 바뀐 기사 {len(moved)}건")
        print("\n".join(moved))
    else:
        print("판정이 바뀐 기사 없음 — 임계값을 그대로 둬도 된다.")

    print("\n신 방식 값 분포 (임계값 후보 찾기)")
    _spread("고유어 종류 수", new_core, new_pass)
    _spread("도메인 종류 수", new_domain, new_pass)
    print(
        "\n통과 집단의 최솟값이 현재 임계값보다 한참 높으면 기준을 올릴 여지가,\n"
        "탈락 집단의 최댓값이 임계값 바로 아래에 붙어 있으면 내릴 여지가 있다.\n"
    )


if __name__ == "__main__":
    main()
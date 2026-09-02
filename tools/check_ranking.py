"""랭킹 필터 진단 (Notion에 이미 저장된 기사로 임계값을 점검한다).

왜 필요한가
----------
is_on_topic() 의 임계값은 실제 기사 분포를 보고 정해야 한다. 합성 예제로
맞춘 값은 실제 데이터에서 어긋난다. 이 도구는 Notion에 쌓인 기사를 그대로
읽어 각 기사가 어느 관문에서 걸리는지 숫자로 보여준다.

Notion 상태를 바꾸지 않는다. 읽기만 한다.

사용법
-----
    # 기본: '선정됨' 기사를 진단
    uv run python -m tools.check_ranking

    # 다른 상태도 함께
    uv run python -m tools.check_ranking --status 선정됨 --status 수집됨

    # 통과한 기사만 / 제외된 기사만
    uv run python -m tools.check_ranking --only pass
    uv run python -m tools.check_ranking --only fail

    # 임계값을 바꿔가며 실험 (파일을 고치지 않고)
    uv run python -m tools.check_ranking --core 3 --density 1.0 --density-no-title 2.0

    # 어떤 단어가 태권도 고유어로 잡혔는지 보기
    # (지표상 정상 기사와 구분되지 않는 기사의 원인을 찾을 때)
    uv run python -m tools.check_ranking --words
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.article_models import Article  # noqa: E402
from core.logger import setup  # noqa: E402
from agents.ranking import ranking_agent as R  # noqa: E402
from tools.notion_store import (  # noqa: E402
    STATUS_DEFAULT,
    fetch_by_status,
    fetch_page_body,
    resolve_data_source_id,
)


def load_articles(statuses: list[str]) -> list[Article]:
    """Notion에서 기사를 읽어 Article 로 만든다. 본문은 페이지 블록에서 가져온다."""
    ds = resolve_data_source_id()
    out: list[Article] = []

    for status in statuses:
        items = fetch_by_status(ds, status)
        print(f"  '{status}' {len(items)}건 조회")
        for it in items:
            body = fetch_page_body(it["page_id"])
            a = Article(title=it["title"], url=it.get("url", ""), source="notion")
            a.body_clean = body
            a.page_id = it["page_id"]
            out.append(a)

    return out


def core_words(article: Article) -> list[str]:
    """이 기사에서 태권도 고유어로 인정된 단어들."""
    tokens = set(R.tokenize(f"{article.title}\n{article.body_clean}"))
    return sorted(tk for tk in tokens if R._is_core(tk))


def diagnose(articles: list[Article], only: str, show_words: bool) -> None:
    rows = []
    for a in articles:
        core = R.count_core_hits(a)
        domain = R.count_domain_hits(a)
        in_title = R.has_domain_in_title(a)
        chars = len(a.body_clean)
        density = core / (chars / 1000) if chars else 0.0
        passed, why = R.is_on_topic(a)
        words = core_words(a) if show_words else []
        rows.append((passed, a.title, core, domain, in_title, chars, density, why, words))

    rows.sort(key=lambda r: (r[0], r[6]))

    print()
    print(f"{'':2} {'고유':>4} {'도메인':>5} {'제목':>4} {'본문':>7} {'밀도':>6}  제목 / 사유")
    print("-" * 110)

    for passed, title, core, domain, in_title, chars, density, why, words in rows:
        if only == "pass" and not passed:
            continue
        if only == "fail" and passed:
            continue
        mark = "O" if passed else "X"
        t = "O" if in_title else "-"
        print(f"{mark:2} {core:>4} {domain:>5} {t:>4} {chars:>7,} {density:>6.2f}  {title[:52]}")
        if not passed:
            print(f"{'':>32}   └ {why}")
        if words:
            # 고유어가 무엇인지 보면 오탐의 원인이 드러난다.
            # (예: 종합 기사에서 '태권도부' 한 단락 때문에 6개가 잡히는 경우)
            print(f"{'':>32}   · {', '.join(words)}")

    ok = sum(1 for r in rows if r[0])
    print("-" * 110)
    print(f"통과 {ok}건 · 제외 {len(rows) - ok}건 (전체 {len(rows)}건)")

    # 임계값 근처 기사를 따로 보여준다. 여기가 조정 여지가 있는 구간이다.
    print()
    print("[경계선 기사] 임계값을 조금만 움직여도 판정이 바뀌는 것들")
    border = [
        r for r in rows
        if abs(r[2] - R.MIN_CORE_HITS) <= 1
        or (r[5] >= R.DENSITY_CHECK_MIN_CHARS and abs(r[6] - (
            R.MIN_CORE_DENSITY if r[4] else R.MIN_CORE_DENSITY_NO_TITLE)) <= 0.4)
    ]
    if not border:
        print("  없음 (모든 기사가 임계값에서 충분히 떨어져 있음)")
    for passed, title, core, domain, in_title, chars, density, why, _w in border[:15]:
        print(f"  {'통과' if passed else '제외'} · 고유 {core}개 · 밀도 {density:.2f} · {title[:50]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="랭킹 필터 진단 (읽기 전용)")
    ap.add_argument("--status", action="append", default=None,
                    help=f"진단할 Notion 상태 (기본 '{STATUS_DEFAULT}'). 여러 번 지정 가능")
    ap.add_argument("--only", choices=["all", "pass", "fail"], default="all")
    ap.add_argument("--words", action="store_true",
                    help="기사별로 태권도 고유어로 인정된 단어를 함께 출력")
    ap.add_argument("--core", type=int, default=None, help="MIN_CORE_HITS 임시 변경")
    ap.add_argument("--density", type=float, default=None, help="MIN_CORE_DENSITY 임시 변경")
    ap.add_argument("--density-no-title", type=float, default=None,
                    help="MIN_CORE_DENSITY_NO_TITLE 임시 변경")
    args = ap.parse_args()

    setup()

    # 임계값을 실행 시점에만 덮어쓴다. 파일은 건드리지 않는다.
    if args.core is not None:
        R.MIN_CORE_HITS = args.core
    if args.density is not None:
        R.MIN_CORE_DENSITY = args.density
    if args.density_no_title is not None:
        R.MIN_CORE_DENSITY_NO_TITLE = args.density_no_title

    print("=" * 60)
    print("랭킹 필터 진단")
    print(f"  고유어 최소 {R.MIN_CORE_HITS}개")
    print(f"  밀도 기준 제목O {R.MIN_CORE_DENSITY} / 제목X {R.MIN_CORE_DENSITY_NO_TITLE}"
          f" (본문 {R.DENSITY_CHECK_MIN_CHARS:,}자 이상일 때)")
    print(f"  도메인 개수 제목O {R.MIN_DOMAIN_HITS} / 제목X {R.MIN_DOMAIN_HITS_NO_TITLE}")
    print("=" * 60)

    statuses = args.status or [STATUS_DEFAULT]
    articles = load_articles(statuses)
    if not articles:
        print("진단할 기사가 없습니다.")
        return 1

    diagnose(articles, args.only, args.words)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
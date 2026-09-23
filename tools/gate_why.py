"""주제 관문 해부 — 특정 기사가 is_on_topic() 을 왜 통과(탈락)했는지 본다.

읽기 전용 오프라인 도구다. 파이프라인과 같은 is_on_topic() 을 부른다.

    uv run python -m tools.gate_why --file data/processed/collected_20260922_235453.csv --grep 창녕
    uv run python -m tools.gate_why --file ... --grep 창녕 생활체육

check_ranking.py 와 다른 점
-------------------------
check_ranking 은 모든 기사의 통과 여부를 한 줄씩 훑는다. 이 도구는 몇 건만
골라 관문마다 값과 기준을 나란히 놓고, '어느 단어가 고유어로 잡혔는지' 와
그 단어가 원문 어디에 있었는지까지 보여 준다. 고유어는 원문 부분 문자열로
세므로('도서관장' ⊃ '관장'), 통과 이유가 엉뚱한 단어일 때 여기서 드러난다.

왜 필요한가 (2026-09-23)
-----------------------
'창녕군 생활체육대회' 기사 4건이 주제 관문을 통과해 대회 레인 3번에 뽑혔다.
태권도가 여러 종목 중 하나로 나오는 군 체육대회 기사다.
"""
import argparse
from pathlib import Path

from agents.collecting.body_cleaner import clean_all
from agents.ranking import ranking_agent as R
from tools.sweep import _latest_csv, _load

CONTEXT = 12  # 고유어 앞뒤로 보여 줄 글자 수


def _where(text: str, word: str) -> str:
    """원문에서 word 가 처음 나온 자리 앞뒤."""
    i = text.find(word)
    if i < 0:
        return ""
    s, e = max(0, i - CONTEXT), min(len(text), i + len(word) + CONTEXT)
    return text[s:e].replace("\n", " ")


def explain(a) -> None:
    text = R._match_text(a)
    chars = len(a.body_clean)
    in_title = R.has_domain_in_title(a)
    core_words = sorted(w for w in R.CORE_KEYWORDS if w in text)
    generic_words = sorted(w for w in R.GENERIC_KEYWORDS if w in text)
    core, mentions = len(core_words), R.count_core_mentions(a)
    spans = R.count_core_spans(a)
    hits = R.count_domain_hits(a)
    ok, reason = R.is_on_topic(a)

    print(f"■ [{'통과' if ok else '탈락'}] {a.title[:70]}")
    print(f"  {a.search_keyword} {a.search_rank}위 · 본문 {chars:,}자 · 제목 고유어 {'O' if in_title else 'X'}")
    print(f"  판정 사유: {reason}\n")

    inc, why = R.is_incident(a)
    print(f"  0) 사건 기사      {'해당 — ' + why if inc else '아님'}")
    print(f"  1) 본문 길이      {chars:,}자 / 최소 {R.MIN_BODY_CHARS}자")
    print(
        f"  2) 핵심어 양      고유어 {core}종·{spans}자리 / 최소 {R.MIN_CORE_HITS}종·"
        f"{R.MIN_CORE_SPANS}자리  또는  언급 {mentions}회 / 최소 {R.MIN_CORE_MENTIONS}회"
    )
    if chars >= R.DENSITY_CHECK_MIN_CHARS:
        min_core = R.MIN_CORE_DENSITY if in_title else R.MIN_CORE_DENSITY_NO_TITLE
        min_mention = R.MIN_MENTION_DENSITY if in_title else R.MIN_MENTION_DENSITY_NO_TITLE
        k = chars / 1000
        print(
            f"  3) 핵심어 밀도    고유어 {core / k:.2f} / 기준 {min_core}  "
            f"또는  언급 {mentions / k:.2f} / 기준 {min_mention}"
        )
    else:
        print(f"  3) 핵심어 밀도    건너뜀 (본문 {R.DENSITY_CHECK_MIN_CHARS}자 미만)")
    required = R.MIN_DOMAIN_HITS if in_title else R.MIN_DOMAIN_HITS_NO_TITLE
    print(f"  4) 도메인 개수    {hits}개 / 기준 {required}개\n")

    print(f"  잡힌 고유어 {core}종")
    for w in core_words:
        print(f"    {w:<8} …{_where(text, w)}…")
    print(f"  잡힌 일반어 {len(generic_words)}종: {', '.join(generic_words)}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, help="collected_*.csv (기본: 가장 최근)")
    ap.add_argument("--grep", nargs="+", required=True, help="제목에 이 문자열 중 하나라도 있는 기사")
    args = ap.parse_args()

    path = args.file or _latest_csv()
    if not path:
        raise SystemExit("data/processed/collected_*.csv 가 없습니다")
    articles = clean_all(_load(path))
    picked = [a for a in articles if any(g in a.title for g in args.grep)]
    print(f"{path.name} · 기사 {len(articles)}건 중 {len(picked)}건\n")
    print(f"기준: {R.threshold_summary()}\n")
    for a in picked:
        explain(a)


if __name__ == "__main__":
    main()
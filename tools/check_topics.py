#!/usr/bin/env python
"""수집 주제 구성 진단 — 수집된 뉴스가 무슨 이야기로 들어오는지 센다.

    py tools/check_topics.py                  가장 최근 collected_*.csv
    py tools/check_topics.py --all            data/processed 의 모든 회차 합산
    py tools/check_topics.py --days 7         최근 7일치 회차 합산
    py tools/check_topics.py --keyword 태권도   그 검색 키워드만
    py tools/check_topics.py --samples 5       카테고리마다 제목 5개씩 함께
    py tools/check_topics.py --with-body       본문까지 보고 분류 (기본은 제목+요약)
    py tools/check_topics.py --file data/processed/collected_20260918_130032.csv

무엇을 세는가
------------
    1. 전체 수집 건수        — 검색 키워드(레인)별로 나눠서
    2. '대회' 관련 건수       — 전체 대비 비율까지
    3. 그 외 주제별 건수      — 승단·심사 / 조직·행정 / 교육 / 시범·행사 ...

왜 필요한가
----------
키워드는 '태권도'와 '태권도조직' 둘뿐인데, 그 안에 들어오는 기사는
대회 결과, 협회 인사, 승품심사 공고, 도장 소식이 섞여 있다. 어느 주제가
몇 %인지 모르면 키워드를 쪼갤지(대회 전용 레인을 팔지), 블로그 카테고리를
몇 개로 나눌지 판단할 근거가 없다.

분류는 사전 매칭이다. LLM 을 부르지 않는다
---------------------------------------
진단 도구는 같은 CSV 를 두 번 돌렸을 때 같은 숫자가 나와야 한다. 숫자가
흔들리면 "대회가 늘었다"가 실제 변화인지 모델 변덕인지 구분할 수 없다.
그래서 TOPIC_RULES 의 문자열 매칭으로만 판정한다.

한 기사는 한 카테고리에만 센다. TOPIC_RULES 위에서부터 먼저 걸리는 쪽이
이긴다. '대회'를 맨 위에 둔 이유는, 대회 기사에 협회명과 심사 얘기가
함께 나오는 일이 흔해서다. "국기원장이 참석한 전국대회"는 대회로 센다.

CSV 는 정제 전 본문을 담고 있다. --with-body 를 주면 body_cleaner 를
그대로 돌려 파이프라인과 같은 조건으로 본다.
"""
import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.collecting.body_cleaner import clean_all           # noqa: E402
from config.settings import PROCESSED_DIR                      # noqa: E402
from core.article_models import Article, canonical_url         # noqa: E402
from core.logger import setup                                  # noqa: E402

# ── 주제 사전 ────────────────────────────────────────────
# 위에서부터 먼저 걸리는 카테고리가 이긴다. 순서가 곧 우선순위다.
# 어느 것에도 안 걸리면 '기타'로 센다. '기타'가 20%를 넘으면 사전을
# 고칠 때다 — 도구가 실제 유입을 못 따라가고 있다는 뜻이다.
TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("대회", (
        "대회", "선수권", "챔피언십", "그랑프리", "올림픽", "아시안게임",
        "전국체전", "체전", "예선", "결선", "본선", "출전", "우승", "준우승",
        "금메달", "은메달", "동메달", "메달", "입상", "시상", "종합우승",
        "겨루기 경기", "품새 경기", "경기력", "국가대표 선발",
    )),
    ("승단·심사", (
        "승단", "승품", "심사", "단증", "품증", "국기원 단증", "자격검정",
    )),
    ("조직·행정", (
        "협회", "국기원", "연맹", "총회", "이사회", "대의원", "회장",
        "이사장", "선임", "임명", "위촉", "취임", "정관", "감사", "징계",
        "집행부", "사무처", "조직위",
    )),
    ("교육·지도자", (
        "연수", "지도자", "사범", "교육과정", "워크숍", "세미나", "강습",
        "자격증", "양성", "직무교육",
    )),
    ("시범·행사", (
        "시범단", "시범", "공연", "축제", "페스티벌", "한마당", "발대식",
        "개막식", "폐막식", "기념식", "행사", "체험",
    )),
    ("도장·수련", (
        "도장", "관장", "수련생", "품새 수업", "유치부", "방과후",
    )),
    ("정책·지원", (
        "예산", "지원금", "조례", "협약", "업무협약", "MOU", "후원",
        "기부", "장학", "공모사업",
    )),
    ("보급·해외", (
        "보급", "해외", "파견", "국제교류", "재외", "세계화", "수출",
    )),
    ("사건·사고", (
        "논란", "고발", "고소", "수사", "폭행", "판결", "재판", "의혹",
        "비리", "성추행", "학대",
    )),
]

OTHER = "기타"


def classify(text: str) -> tuple[str, list[str]]:
    """주제 카테고리와 걸린 단어를 돌려준다. 먼저 걸리는 카테고리가 이긴다."""
    for name, words in TOPIC_RULES:
        hit = [w for w in words if w in text]
        if hit:
            return name, hit
    return OTHER, []


def text_of(a: Article, with_body: bool) -> str:
    """분류 대상 텍스트. 기본은 제목+요약이다.

    본문까지 넣으면 기사 말미의 관련기사 문구나 언론사 소개에 '대회'가
    스쳐도 대회로 잡힌다. 제목+요약은 그 기사가 무엇을 다루는지 압축한
    자리라 오탐이 적다. 본문 기준이 궁금하면 --with-body 로 비교한다.
    """
    parts = [a.title, a.summary]
    if with_body:
        parts.append(a.body_clean or a.body)
    return " ".join(p for p in parts if p)


# ── CSV 로드 ─────────────────────────────────────────────
def _csv_files(args) -> list[Path]:
    """볼 파일 목록. --file > --days > --all > 최근 1개 순으로 결정한다."""
    if args.file:
        return [Path(args.file)]
    files = sorted(PROCESSED_DIR.glob(f"{args.prefix}_*.csv"))
    if not files:
        return []
    if args.days:
        cutoff = datetime.now() - timedelta(days=args.days)
        picked = []
        for f in files:
            stamp = f.stem.split("_", 1)[1]  # collected_20260918_130032 → 20260918_130032
            try:
                when = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
            except ValueError:
                continue                      # 이름 규칙이 다른 파일은 건너뛴다
            if when >= cutoff:
                picked.append(f)
        return picked
    return files if args.all else files[-1:]


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


def load_all(paths: list[Path], dedupe_across: bool) -> list[Article]:
    """여러 회차를 합친다.

    LOOKBACK_DAYS 만큼 겹쳐 수집하므로 같은 기사가 여러 회차 CSV 에 있다.
    합산할 때 그대로 세면 대회 기사처럼 오래 회자되는 건이 부풀려진다.
    URL 로 한 번만 센다 (--no-dedupe 로 끌 수 있다).
    """
    seen: set[str] = set()
    merged: list[Article] = []
    for p in paths:
        for a in _load(p):
            key = canonical_url(a.url)
            if dedupe_across and key in seen:
                continue
            seen.add(key)
            merged.append(a)
    return merged


# ── 출력 ─────────────────────────────────────────────────
def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:5.1f}%" if total else "    -"


def print_table(
    counts: dict[str, Counter], lanes: list[str], total: int, head: str
) -> None:
    """카테고리 × 레인 표. 레인 목록은 실제 데이터에서 뽑는다.

    키워드를 '태권도협회·국기원·세계태권도연맹'으로 쪼개도 열이 따라
    늘어난다. 키워드를 코드에 박아 두면 그때마다 도구를 고쳐야 한다.
    """
    width = max([len(head)] + [len(k) for k in counts] + [6])
    lane_w = [max(len(l), 6) for l in lanes]

    header = head.ljust(width) + " │ " + " ".join(
        l.rjust(w) for l, w in zip(lanes, lane_w)
    )
    header += " │ " + "합계".rjust(6) + "  비율"
    print(header)
    print("─" * len(header))

    for name, per_lane in counts.items():
        row_total = sum(per_lane.values())
        line = name.ljust(width) + " │ " + " ".join(
            str(per_lane.get(l, 0)).rjust(w) for l, w in zip(lanes, lane_w)
        )
        line += " │ " + f"{row_total:,}".rjust(6) + f" {_pct(row_total, total)}"
        print(line)


def main() -> None:
    p = argparse.ArgumentParser(description="수집 주제 구성 진단")
    p.add_argument("--file", help="CSV 경로 (기본: 가장 최근 파일)")
    p.add_argument("--all", action="store_true", help="모든 회차를 합산")
    p.add_argument("--days", type=int, help="최근 N일치 회차만 합산")
    p.add_argument("--prefix", default="collected",
                   help="collected(수집분 전체) | selected(선정분). 기본 collected")
    p.add_argument("--keyword", help="이 검색 키워드의 기사만 본다")
    p.add_argument("--samples", type=int, default=0,
                   help="카테고리마다 제목 N개를 함께 출력")
    p.add_argument("--with-body", action="store_true",
                   help="본문까지 보고 분류 (기본: 제목+요약)")
    p.add_argument("--no-dedupe", action="store_true",
                   help="회차 간 같은 URL 도 각각 센다")
    args = p.parse_args()

    setup()
    paths = [p for p in _csv_files(args) if p.exists()]
    if not paths:
        print(f"CSV 를 찾을 수 없습니다: {args.file or PROCESSED_DIR}")
        raise SystemExit(1)

    articles = load_all(paths, dedupe_across=not args.no_dedupe)
    if args.with_body:
        articles = clean_all(articles)
    if args.keyword:
        articles = [a for a in articles if a.search_keyword == args.keyword]
    if not articles:
        print("대상 기사가 없습니다.")
        raise SystemExit(1)

    # 레인 목록은 데이터에서 뽑되, 수집 순서(먼저 나온 키워드)를 지킨다
    lanes: list[str] = []
    for a in articles:
        kw = a.search_keyword or "-"
        if kw not in lanes:
            lanes.append(kw)

    per_lane_total = Counter(a.search_keyword or "-" for a in articles)
    counts: dict[str, Counter] = {}
    samples: dict[str, list[tuple[str, str, list[str]]]] = {}
    for a in articles:
        name, hit = classify(text_of(a, args.with_body))
        counts.setdefault(name, Counter())[a.search_keyword or "-"] += 1
        samples.setdefault(name, []).append((a.search_keyword or "-", a.title, hit))

    total = len(articles)
    span = f"{paths[0].name} ~ {paths[-1].name}" if len(paths) > 1 else paths[0].name
    basis = "제목+요약+본문" if args.with_body else "제목+요약"
    dedup = "URL 중복 1건으로" if not args.no_dedupe else "중복 그대로"

    print(f"\n파일 {len(paths)}개: {span}")
    print(f"기준: {basis} 매칭 · {dedup}\n")

    # 1. 전체
    print(f"전체 수집 건수  {total:,}건")
    for l in lanes:
        n = per_lane_total[l]
        print(f"  {l:<10} {n:>5,}건  {_pct(n, total)}")

    # 2. 대회
    race = counts.get("대회", Counter())
    race_n = sum(race.values())
    print(f"\n'대회' 관련     {race_n:,}건  ({_pct(race_n, total).strip()})")
    for l in lanes:
        n = race.get(l, 0)
        print(f"  {l:<10} {n:>5,}건  (레인 내 {_pct(n, per_lane_total[l]).strip()})")

    # 3. 대회 외
    rest = {k: v for k, v in counts.items() if k != "대회"}
    order = [name for name, _ in TOPIC_RULES if name in rest] + (
        [OTHER] if OTHER in rest else []
    )
    rest_sorted = {k: rest[k] for k in order}
    rest_n = sum(sum(c.values()) for c in rest_sorted.values())
    print(f"\n'대회' 외 주제별  {rest_n:,}건")
    print_table(rest_sorted, lanes, total, "주제")

    other_n = sum(counts.get(OTHER, Counter()).values())
    if total and other_n / total > 0.2:
        print(f"\n※ '기타'가 {_pct(other_n, total).strip()} 입니다. "
              "TOPIC_RULES 에 빠진 주제가 있는지 확인하세요.")

    # 4. 샘플
    if args.samples:
        print("\n── 샘플 ──")
        for name in ["대회"] + order:
            rows = samples.get(name, [])[: args.samples]
            if not rows:
                continue
            print(f"\n[{name}] {sum(counts[name].values()):,}건")
            for lane, title, hit in rows:
                tag = ",".join(hit[:3]) if hit else "-"
                print(f"  {lane:<8} | {tag:<14} | {title[:44]}")


if __name__ == "__main__":
    main()
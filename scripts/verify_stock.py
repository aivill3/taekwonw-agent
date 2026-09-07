"""스톡 사진 등록 검증 — 새로 받은 사진을 인덱스에 넣기 전에 거른다.

왜 필요한가
-----------
Pexels 에 태권도 전용 사진은 얕다. 질의가 무엇이든 다른 종목이 섞여
들어온다. 실측으로 확인된 것만 해도 농구, 필라테스, 요가, 웨이트,
아이스하키, 복싱, 테니스가 있었다.

종목이 틀린 사진이 인덱스에 들어가면 CLIP 검색이 그걸 골라 쓰고,
태권도 기사에 농구공을 든 사람이 붙는다. 검색 단계에서는 이미 늦다.

    fetch_stock.py  →  [verify_stock.py]  →  build_index.py

카테고리 자동 채택을 두지 않는 이유
--------------------------------
처음에는 '트로피·빈 체육관은 종목 무관'이라고 보고 자동 채택했다.
틀렸다. 같은 카테고리라도 질의마다 갈린다.

    trophy close up      → 사물만. 문제 없음
    gold medal athlete   → 농구 유니폼에 농구공을 든 인물이 나왔다
    yoga studio empty    → 요가 스튜디오. 배경으로 종목이 드러난다

카테고리 단위로는 안전을 보장할 수 없다. 300장을 모두 눈으로 본다.
장당 2~3초면 되므로 20분 안팎이다.

판정 기준
--------
아래 순서로 보고, 하나라도 걸리면 배제한다.

    1. 배경   다른 종목의 시설·기구가 보이는가
              (필라테스 리포머, 요가 소품, 웨이트 기구, 러닝머신,
               농구 코트, 아이스링크, 복싱 링, 테니스 코트, 수영장,
               잔디 필드, 트랙)
    2. 복장   옷이 조금이라도 보이는데 도복이 아닌가
              옷이 전혀 안 보이면(메달 클로즈업, 실루엣, 뒷모습) 통과.
              손·발 부분컷은 소매나 발목 바지가 프레임에 들어왔는지로
              가른다. 레깅스·트레이닝복이 보이면 배제.
    3. 장비   다른 종목 장비가 있는가 (공, 라켓, 배트, 글러브, 자전거)
    4. 애매   무술 사진인데 태권도인지 가라테인지 모르겠는가 → '?'
    5. 나머지 → 통과

쓰는 법
-------
    # 1) 판정할 목록을 CSV 로 뽑는다
    python scripts/verify_stock.py init --dir data/stock_pending

    # 2) 사람이 CSV 의 '판정' 칸을 채운다
    #      o  통과      x  배제      ?  애매 (신호 칸을 채운다)

    # 3) 채운 CSV 를 되먹여 분류한다
    python scripts/verify_stock.py apply --dir data/stock_pending
    python scripts/verify_stock.py apply --dir data/stock_pending --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import date
from pathlib import Path

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}

# ── 1차 판정 ────────────────────────────────────────────────────────
PASS_MARKS = {"o", "O", "ㅇ", "y", "Y", "1", "v", "V"}
FAIL_MARKS = {"x", "X", "ㅌ", "n", "N", "0"}
DOUBT_MARKS = {"?", "/", "-", "ㅁ"}

# 배제 사유. 집계해서 다음 수집 때 질의를 고치는 근거로 쓴다.
REASONS = {
    "배경": "다른 종목 시설·기구",
    "복장": "도복이 아닌 옷",
    "종목": "다른 종목 장비",
    "품질": "해상도·구도·선명도",
    "기타": "그 밖",
}

# ── 2차 점수표 ('?' 로 표시한 것만 쓴다) ──────────────────────────────
#
# 가산점만으로는 뚫린다. 유도 신호가 태권도 점수를 깎지 않으면
# 'V넥 + 헤드기어 + 옷깃 잡음' 같은 사진이 태권도로 판정된다.
# 그래서 잡기 계열은 점수가 아니라 즉시 배제로 뺐다.
#
# '허리띠가 보임'은 뺐다. 세 종목 다 띠를 매므로 변별력이 없으면서
# 가라테·유도 쪽만 올려주는 노이즈다.

VETO = {
    "깃잡음": "도복 깃을 잡고 있음",
    "메치기시도": "상대를 잡아 메치려는 동작",
    "메치기기술": "업어치기·허리후리기 등 메치기",
}

TAEKWONDO = {
    "V넥": 3,        # WT 경기복은 앞이 트이지 않은 풀오버 — 태권도 확정 신호
    "색상트림": 2,    # 깃의 검정/청홍 배색. V넥과 독립 가산
    "전자호구": 5,
    "헤드기어": 4,
    "높은발차기": 2,
    "회전발차기": 2,
    "낮은자세손기술": -2,
}

KARATE = {
    "손기술중심": 2,
    "낮은자세손기술": 3,
    "깃형여밈": 3,
    "두꺼운캔버스": 2,
}

JUDO = {
    "깃형여밈": 1,
    "두꺼운캔버스": 2,
}

SIGNALS = [
    "V넥", "색상트림", "전자호구", "헤드기어", "높은발차기", "회전발차기",
    "손기술중심", "낮은자세손기술", "깃형여밈", "두꺼운캔버스",
]

# 착용 정상성. 종목이 맞아도 좌우 반전된 사진은 뺀다.
# 동아시아 무도복은 모두 왼쪽 자락이 위(우임)로, 정면에서 'y'자로 보인다.
# 반대로 보이면 이미지가 뒤집혔거나 연출 오착용이다.
WEAR_OK = {"정상", "없음", ""}


def mark_of(value: str) -> str:
    """1차 판정 칸을 읽는다. pass / fail / doubt / blank."""
    v = value.strip()
    if not v:
        return "blank"
    if v in PASS_MARKS:
        return "pass"
    if v in FAIL_MARKS:
        return "fail"
    if v in DOUBT_MARKS:
        return "doubt"
    return "doubt"  # 뭐라고 썼든 애매한 것으로 본다


def is_on(value: str) -> bool:
    return bool(value.strip())


def score(row: dict) -> tuple[int, int, int]:
    tkd = sum(w for k, w in TAEKWONDO.items() if is_on(row.get(k, "")))
    kar = sum(w for k, w in KARATE.items() if is_on(row.get(k, "")))
    jud = sum(w for k, w in JUDO.items() if is_on(row.get(k, "")))
    return tkd, kar, jud


def judge(row: dict) -> tuple[str, str]:
    """판정과 사유를 낸다."""
    mark = mark_of(row.get("판정", ""))

    if mark == "blank":
        return "보류", "미판정"

    if mark == "fail":
        reason = (row.get("사유") or "").strip()
        label = REASONS.get(reason, reason or "사유 미기재")
        return "배제", label

    if mark == "pass":
        return "채택", "육안 통과"

    # '?' — 신호 칸으로 채점한다.
    hit = [label for key, label in VETO.items() if is_on(row.get(key, ""))]
    if hit:
        return "배제", f"VETO — {hit[0]}"

    wear = (row.get("여밈") or "").strip()
    if wear and wear not in WEAR_OK:
        return "배제", "좌우 반전 또는 오착용"

    tkd, kar, jud = score(row)
    if tkd == kar == jud == 0:
        return "보류", "'?' 인데 신호가 비어 있음"

    rival = max(kar, jud)
    gap = tkd - rival
    detail = f"태권도 {tkd} / 가라테 {kar} / 유도 {jud}"

    if tkd >= 5 and gap >= 3:
        return "채택", detail
    if tkd >= 3:
        return "보류", f"{detail} — 격차 {gap}"
    return "배제", detail


def collect_images(root: Path) -> list[tuple[Path, str]]:
    """이미지와 카테고리를 모은다. 하위 폴더명을 카테고리로 본다."""
    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in EXTS:
            rel = path.relative_to(root)
            category = rel.parts[0] if len(rel.parts) > 1 else ""
            found.append((path, category))
    return found


def load_sources(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  ! {path} 를 읽지 못했습니다. 빈 상태로 시작합니다.")
        return {}


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    if not root.is_dir():
        print(f"폴더가 없습니다: {root}")
        return 1

    images = collect_images(root)
    if not images:
        print(f"{root} 에 이미지가 없습니다.")
        return 1

    sources = load_sources(Path(args.sources))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    header = ["파일명", "카테고리", "판정", "사유",
              *VETO.keys(), *SIGNALS, "여밈", "질의", "메모"]

    by_category: dict[str, int] = {}
    # utf-8-sig — Excel 이 BOM 없이는 한글을 깨뜨린다.
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for path, category in images:
            name = str(path.relative_to(root))
            meta = sources.get(path.name, {})
            if not category:
                category = meta.get("category", "")
            by_category[category] = by_category.get(category, 0) + 1
            row = {"파일명": name, "카테고리": category, "질의": meta.get("query", "")}
            writer.writerow([row.get(col, "") for col in header])

    print(f"목록을 만들었습니다: {out}")
    print(f"  전체 {len(images)}장")
    for category, count in sorted(by_category.items(), key=lambda x: -x[1]):
        print(f"    {category or '(미분류)':<10} {count:>4}")
    print()
    print("'판정' 칸을 채웁니다.  o 통과 / x 배제 / ? 애매")
    print("  x 는 '사유' 칸에 배경·복장·종목·품질 중 하나를 적으면")
    print("  어느 질의가 나빴는지 집계됩니다.")
    print("  ? 는 신호 칸을 채우면 점수로 판정합니다.")
    print()
    print("판정 순서 (하나라도 걸리면 x)")
    print("  1. 배경에 다른 종목 시설·기구가 보이나")
    print("  2. 옷이 보이는데 도복이 아닌가")
    print("  3. 다른 종목 장비가 있나")
    print("  4. 무술인데 태권도인지 애매한가 → ?")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV 가 없습니다: {csv_path}")
        print("먼저 init 으로 목록을 만드세요.")
        return 1

    root = Path(args.dir)
    accept_dir = Path(args.accept)
    reject_dir = Path(args.reject)
    sources_path = Path(args.sources)
    sources = load_sources(sources_path)
    today = date.today().isoformat()

    with csv_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("CSV 가 비어 있습니다.")
        return 1

    buckets: dict[str, list[tuple[str, str]]] = {"채택": [], "보류": [], "배제": []}
    moves: list[tuple[Path, Path]] = []
    missing = 0
    # 배제 사유를 질의별로 모은다. 다음 수집 때 어느 질의를 뺄지 판단한다.
    by_query: dict[str, dict[str, int]] = {}

    for row in rows:
        name = (row.get("파일명") or "").strip()
        if not name:
            continue
        src = root / name
        if not src.exists():
            missing += 1
            continue

        verdict, reason = judge(row)
        buckets[verdict].append((name, reason))

        if verdict == "채택":
            moves.append((src, accept_dir / src.name))
        elif verdict == "배제":
            moves.append((src, reject_dir / src.name))
            query = (row.get("질의") or "").strip() or "(미기록)"
            tally = by_query.setdefault(query, {"배제": 0, "전체": 0})
            tally["배제"] += 1
        # 보류는 그 자리에 둔다. 다시 볼 대상이므로 옮기면 찾기 어렵다.

        query = (row.get("질의") or "").strip() or "(미기록)"
        by_query.setdefault(query, {"배제": 0, "전체": 0})["전체"] += 1

        tkd, kar, jud = score(row)
        entry = sources.get(src.name, {})
        entry.update({
            "category": (row.get("카테고리") or "").strip(),
            "verified": today,
            "verdict": verdict,
            "reason": reason,
        })
        if (tkd, kar, jud) != (0, 0, 0):
            entry["score"] = {"taekwondo": tkd, "karate": kar, "judo": jud}
        if (memo := (row.get("메모") or "").strip()):
            entry["memo"] = memo
        sources[src.name] = entry

    total = sum(len(v) for v in buckets.values())
    print(f"판정 결과 — 전체 {total}장")
    for verdict in ("채택", "보류", "배제"):
        n = len(buckets[verdict])
        pct = f"{n / total * 100:.0f}%" if total else "-"
        print(f"  {verdict} {n}장 ({pct})")
    if missing:
        print(f"  ! 파일을 찾지 못한 행 {missing}개")

    if buckets["보류"]:
        print("\n[보류]")
        for name, reason in buckets["보류"]:
            print(f"  {name}  ← {reason}")

    if by_query:
        print("\n질의별 배제율 — 다음 수집에서 뺄 질의를 고르는 근거")
        rows_out = sorted(by_query.items(),
                          key=lambda x: -(x[1]["배제"] / max(x[1]["전체"], 1)))
        for query, tally in rows_out:
            if not tally["전체"]:
                continue
            rate = tally["배제"] / tally["전체"] * 100
            bar = "█" * round(rate / 10)
            print(f"  {query:<32} {tally['배제']:>3}/{tally['전체']:<3} {rate:>3.0f}% {bar}")

    if not args.apply:
        print("\n미리보기입니다. 실제로 옮기려면 --apply 를 붙이세요.")
        return 0

    accept_dir.mkdir(parents=True, exist_ok=True)
    reject_dir.mkdir(parents=True, exist_ok=True)
    for src, dst in moves:
        if dst.exists():
            print(f"  건너뜀 (이미 있음): {dst.name}")
            continue
        shutil.move(str(src), str(dst))

    sources_path.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(moves)}장을 옮기고 {sources_path} 에 기록했습니다.")
    print(f"보류 {len(buckets['보류'])}장은 {root} 에 그대로 있습니다.")
    if buckets["채택"]:
        print(f"\n다음: python scripts/build_index.py --dir {accept_dir} --out data/index")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="스톡 사진 등록 검증")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="판정용 CSV 발행")
    p_init.add_argument("--dir", default="data/stock_pending")
    p_init.add_argument("--out", default="data/stock_verify.csv")
    p_init.add_argument("--sources", default="data/stock_sources.json")
    p_init.set_defaults(func=cmd_init)

    p_apply = sub.add_parser("apply", help="채운 CSV 로 분류")
    p_apply.add_argument("--dir", default="data/stock_pending")
    p_apply.add_argument("--csv", default="data/stock_verify.csv")
    p_apply.add_argument("--accept", default="data/stock")
    p_apply.add_argument("--reject", default="data/stock_rejected")
    p_apply.add_argument("--sources", default="data/stock_sources.json")
    p_apply.add_argument("--apply", action="store_true", help="실제로 파일을 옮긴다")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
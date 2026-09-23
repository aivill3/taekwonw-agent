#!/usr/bin/env python
"""주제 게이트 채점 — is_on_topic() 이 라벨 셋에서 몇 점인지 잰다.

    py tools/eval_gate.py                          현재 임계값으로 채점
    py tools/eval_gate.py --errors                 오탐·누락 전체 목록
    py tools/eval_gate.py --sweep core=2,3,4
    py tools/eval_gate.py --sweep core=2,3 mentions=3,4,5 body=200,250
    py tools/eval_gate.py --append                 결과를 한 파일에 쌓기

왜 필요한가
----------
sweep.py 는 '임계값을 바꾸면 무엇이 뽑히는가' 를 보여 준다. 눈으로 읽어야
좋고 나쁨을 안다. 이 파일은 정답(labels.csv)과 대조해 숫자로 답한다.

    오탐(FP)   무관 기사인데 통과했다      ← 이동섭 '제2국기원' 유형
    누락(FN)   조직·대회 기사인데 떨어졌다  ← 이주영 '품새 퀸' 유형

임계값을 어느 방향으로 움직여도 둘 중 하나는 나빠진다. 그 교환비를 보고
정하자는 것이 이 도구의 전부다.

정답 규칙
--------
    조직 · 대회 · 태권도  →  통과해야 한다 (positive)
    무관                 →  떨어져야 한다 (negative)

레인 배정 정확도(조직 기사가 조직 레인으로 갔는지)는 여기서 재지 않는다.
게이트는 통과/탈락만 판정한다. 레인 배정은 키워드가 정하고 있어서,
그 문제는 별도 도구가 필요하다.

sweep.py 와 같은 방식으로 임계값을 바꾼다
-------------------------------------
임계값은 모듈을 읽을 때 os.getenv 로 한 번만 잡힌다. 환경변수를 세팅하고
ranking_agent 를 importlib.reload 한다. 같은 이유로 reload 한 모듈의
속성(R.is_on_topic)으로 불러야 한다 — from ... import 로 가져오면
reload 전의 함수가 그대로 남는다.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import itertools
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import KST, QUALITY_DIR    # noqa: E402
from core.article_models import Article         # noqa: E402

LABELS_PATH = QUALITY_DIR / "labels.csv"

# 라벨 이름 = 레인 이름 = Notion '검색키워드' 값.
ORG = "조직"
EVENT = "대회"
GENERAL = "태권도"
OFF = "무관"
LABELS = (ORG, EVENT, GENERAL, OFF)

# 게이트는 '통과/탈락'만 판정한다. 세 카테고리는 모두 통과해야 하고,
# 무관만 떨어져야 한다. 어느 카테고리로 갈지는 게이트가 정하지 않는다.
POSITIVE = (ORG, EVENT, GENERAL)

# 스윕이 돌리는 손잡이. (인자 이름, 환경변수 이름) — sweep.py 와 같은 목록 중
# 주제 게이트에 영향을 주는 것만 추렸다.
KNOBS = {
    "core": "MIN_CORE_HITS",
    "mentions": "MIN_CORE_MENTIONS",
    "body": "MIN_BODY_CHARS",
    "domain": "MIN_DOMAIN_HITS",
    "domain_no_title": "MIN_DOMAIN_HITS_NO_TITLE",
    "crime": "EXCLUDE_CRIME",
    "dispute": "EXCLUDE_DISPUTE",
}


# ── 라벨 읽기 ──────────────────────────────────────────────
def load_labeled(naver_only: bool = False) -> list[tuple[Article, str]]:
    """labels.csv → [(Article, 정답 라벨)]. 라벨이 빈 행은 제외한다.

    labels.csv 는 정제된 body_clean 을 그대로 품고 있으므로 여기서 다시
    정제하지 않는다. 채점 시점마다 정제 규칙이 달라지면 같은 라벨로
    과거 결과와 비교할 수 없게 된다 — 라벨을 만든 시점의 본문으로 고정한다.
    """
    if not LABELS_PATH.exists():
        print(
            f"라벨 파일이 없습니다: {LABELS_PATH}\n"
            "  py tools/label_corpus.py --llm  으로 먼저 만드세요.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    out: list[tuple[Article, str]] = []
    with LABELS_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip()
            if label not in LABELS:
                continue
            # 구글 RSS 시절 기사는 수집 모집단이 달라 섞으면 안 된다.
            # (2026-09-22: 첫 --limit 8 실행이 필터 없이 돌아 8건 혼입)
            if naver_only and not (row.get("search_keyword") or "").strip():
                continue
            out.append((
                Article(
                    title=row.get("title", ""),
                    url=row.get("url", ""),
                    press=row.get("press", ""),
                    published=row.get("published", ""),
                    search_keyword=row.get("search_keyword", ""),
                    search_rank=int(row.get("search_rank") or 0),
                    body_clean=row.get("body_clean", ""),
                ),
                label,
            ))
    return out


def _reload_ranking(env: dict[str, str]):
    """환경변수를 세팅하고 ranking_agent 를 다시 읽는다."""
    os.environ.update(env)
    import agents.ranking.ranking_agent as R
    importlib.reload(R)
    return R


# ── 채점 ──────────────────────────────────────────────────
class Score:
    """한 조합의 채점 결과."""

    def __init__(self) -> None:
        self.tp: list[tuple[Article, str, str]] = []  # (기사, 정답, 사유)
        self.fp: list[tuple[Article, str, str]] = []
        self.fn: list[tuple[Article, str, str]] = []
        self.tn: list[tuple[Article, str, str]] = []

    @property
    def precision(self) -> float:
        d = len(self.tp) + len(self.fp)
        return len(self.tp) / d if d else 0.0

    @property
    def recall(self) -> float:
        d = len(self.tp) + len(self.fn)
        return len(self.tp) / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def recall_of(self, label: str) -> tuple[int, int]:
        """특정 라벨의 (통과, 전체). 조직과 대회의 누락률이 다른지 본다."""
        hit = sum(1 for _, lb, _ in self.tp if lb == label)
        miss = sum(1 for _, lb, _ in self.fn if lb == label)
        return hit, hit + miss


def grade(data: list[tuple[Article, str]], R) -> Score:
    s = Score()
    for article, truth in data:
        passed, why = R.is_on_topic(article)
        should = truth in POSITIVE
        if should and passed:
            s.tp.append((article, truth, why))
        elif should and not passed:
            s.fn.append((article, truth, why))
        elif not should and passed:
            s.fp.append((article, truth, why))
        else:
            s.tn.append((article, truth, why))
    return s


def _gate_of(reason: str) -> str:
    """탈락 사유 문자열 → 어느 관문에서 떨어졌는지. 집계용 짧은 이름."""
    if reason.startswith("본문 "):
        return "본문 길이"
    if reason.startswith("태권도 고유어 "):
        return "핵심어 양"
    if reason.startswith("고유어 밀도 "):
        return "핵심어 밀도"
    if reason.startswith("도메인 키워드 "):
        return "도메인 개수"
    return "사건 기사"  # is_incident 가 낸 사유 (문구가 여러 가지다)


# ── 출력 ──────────────────────────────────────────────────
def print_report(s: Score, data: list[tuple[Article, str]], show_errors: bool) -> None:
    n = len(data)
    pos = sum(1 for _, lb in data if lb in POSITIVE)
    print(f"\n■ 채점 — 라벨 {n}건 (통과해야 할 기사 {pos}건 · 무관 {n - pos}건)")
    print()
    print("                     판정: 통과   판정: 탈락")
    print(f"  정답 조직·대회·태권도 {len(s.tp):>6}      {len(s.fn):>6}  ← 누락(FN)")
    print(f"  정답 무관             {len(s.fp):>6}      {len(s.tn):>6}")
    print("                      ↑ 오탐(FP)")
    print()
    print(
        f"  정밀도 {s.precision:.1%}  (통과시킨 것 중 진짜 태권도 기사 비율)\n"
        f"  재현율 {s.recall:.1%}  (태권도 기사 중 살아남은 비율)\n"
        f"  F1     {s.f1:.1%}"
    )

    for label in (ORG, EVENT, GENERAL):
        hit, total = s.recall_of(label)
        if total:
            print(f"    {label} 재현율 {hit}/{total} ({hit / total:.0%})")

    if s.fn:
        gates: dict[str, int] = {}
        for _, _, why in s.fn:
            g = _gate_of(why)
            gates[g] = gates.get(g, 0) + 1
        print("\n■ 누락(FN) 사유 — 좋은 기사를 어느 관문이 떨어뜨렸나")
        for g, c in sorted(gates.items(), key=lambda kv: -kv[1]):
            print(f"    {g:<10} {c:>3}건")

    limit = None if show_errors else 8
    if s.fn:
        print(f"\n■ 누락 목록{'' if show_errors else f' (상위 {limit}건)'}")
        for article, truth, why in s.fn[:limit]:
            print(f"    [{truth}] {article.title[:44]}")
            print(f"          {why}")
    if s.fp:
        print(f"\n■ 오탐 목록{'' if show_errors else f' (상위 {limit}건)'}")
        for article, _, why in s.fp[:limit]:
            print(f"    {article.title[:44]}")
            print(f"          {why}")

    if not show_errors and (len(s.fn) > (limit or 0) or len(s.fp) > (limit or 0)):
        print("\n  전체 목록은 --errors")


def print_sweep_table(rows: list[dict], names: list[str]) -> None:
    head = "  ".join(f"{n:>8}" for n in names)
    print(f"\n{head}  {'정밀도':>7} {'재현율':>7} {'F1':>7}  {'FP':>4} {'FN':>4}")
    print("-" * (len(head) + 40))
    for r in rows:
        vals = "  ".join(f"{str(r[n]):>8}" for n in names)
        print(
            f"{vals}  {r['precision']:>6.1%} {r['recall']:>6.1%} {r['f1']:>6.1%}"
            f"  {r['fp']:>4} {r['fn']:>4}"
        )
    best = max(rows, key=lambda r: r["f1"])
    print(
        "\n  F1 최고: "
        + " · ".join(f"{n}={best[n]}" for n in names)
        + f" (F1 {best['f1']:.1%} · FP {best['fp']} · FN {best['fn']})"
    )
    print(
        "\n  읽는 법\n"
        "    FP 를 줄이면 FN 이 는다. 어느 쪽 손실이 큰지는 운영 판단이다.\n"
        "    이 블로그는 하루 8건만 쓰므로 후보가 넉넉하면 FP 를 줄이는 쪽이,\n"
        "    후보가 모자라 4건 묶음을 못 채우면 FN 을 줄이는 쪽이 맞다."
    )


# ── 진입점 ────────────────────────────────────────────────
def _parse_sweep(specs: list[str]) -> dict[str, list[str]]:
    """['core=2,3,4', 'body=200,250'] → {'core': ['2','3','4'], ...}"""
    out: dict[str, list[str]] = {}
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"--sweep 형식은 이름=값,값 입니다: {spec!r}")
        name, raw = spec.split("=", 1)
        name = name.strip()
        if name not in KNOBS:
            raise SystemExit(
                f"모르는 손잡이 {name!r} — 쓸 수 있는 것: {', '.join(KNOBS)}"
            )
        out[name] = [v.strip() for v in raw.split(",") if v.strip()]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="is_on_topic() 을 라벨 셋으로 채점한다")
    p.add_argument("--errors", action="store_true", help="오탐·누락 전체 목록")
    p.add_argument(
        "--sweep", nargs="+", metavar="이름=값,값", default=[],
        help=f"임계값 조합. 이름: {', '.join(KNOBS)}",
    )
    p.add_argument("--append", action="store_true", help="gate_eval.csv 에 이어 쓰기")
    p.add_argument("--out", help="결과 CSV 경로")
    p.add_argument("--verbose", action="store_true", help="ranking_agent 로그 보기")
    p.add_argument(
        "--naver-only", action="store_true",
        help="검색 키워드가 있는 기사만 채점 (= 네이버 수집분)",
    )
    args = p.parse_args()

    # ranking_agent 는 기사마다 탈락 사유를 INFO 로 찍는다. 표가 묻히므로 잠근다.
    logging.getLogger().setLevel(logging.INFO if args.verbose else logging.ERROR)

    data = load_labeled(args.naver_only)
    if not data:
        print("라벨된 기사가 없습니다. py tools/label_corpus.py 로 먼저 만드세요.")
        raise SystemExit(1)
    if len(data) < 100:
        print(f"⚠️ 라벨 {len(data)}건 — 100건 미만이면 수치가 크게 흔들립니다.")

    # 단일 채점
    if not args.sweep:
        R = _reload_ranking({})
        s = grade(data, R)
        # 파이프라인 로그와 같은 함수를 쓴다. 두 곳에서 따로 조립하면
        # 채점 결과와 실제 실행 설정을 대조할 때 미묘하게 어긋난다.
        print(f"\n현재 임계값: {R.threshold_summary()}")
        print_report(s, data, args.errors)
        return

    # 스윕
    axes = _parse_sweep(args.sweep)
    names = list(axes)
    combos = list(itertools.product(*(axes[n] for n in names)))
    print(f"\n라벨 {len(data)}건 · 조합 {len(combos)}개")

    run_at = datetime.now(KST).replace(microsecond=0).isoformat()
    rows: list[dict] = []
    for combo in combos:
        env = {KNOBS[n]: v for n, v in zip(names, combo)}
        R = _reload_ranking(env)
        s = grade(data, R)
        row = {
            "run_at": run_at,
            "labels": len(data),
            **{n: v for n, v in zip(names, combo)},
            "tp": len(s.tp), "fp": len(s.fp), "fn": len(s.fn), "tn": len(s.tn),
            "precision": round(s.precision, 4),
            "recall": round(s.recall, 4),
            "f1": round(s.f1, 4),
        }
        for label in (ORG, EVENT, GENERAL):
            hit, total = s.recall_of(label)
            row[f"recall_{label}"] = round(hit / total, 4) if total else ""
        rows.append(row)

    print_sweep_table(rows, names)

    QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    if args.out:
        out = Path(args.out)
    elif args.append:
        out = QUALITY_DIR / "gate_eval.csv"
    else:
        out = QUALITY_DIR / f"gate_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    exists = out.exists()
    mode = "a" if args.append and exists else "w"
    with out.open(mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if mode == "w" or not exists:
            writer.writeheader()
        writer.writerows(rows)
    print(f"\n결과 {len(rows)}줄 저장: {out}")


if __name__ == "__main__":
    main()
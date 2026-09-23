#!/usr/bin/env python
"""주제 판정 앵커 만들기 — labels.csv → data/quality/anchors.npz

    py tools/build_anchors.py              앵커 생성 + 성능 출력
    py tools/build_anchors.py --dry-run    저장하지 않고 성능만

왜 파일로 굽는가
-------------
앵커를 매 실행 labels.csv 에서 만들면, 라벨을 한 건 고쳤을 뿐인데 그날
발행 결과가 달라진다. 원인을 찾을 때 코드도 설정도 안 바뀐 채 결과만
달라진 상황이 제일 나쁘다. 앵커는 여기서 명시적으로 다시 만들 때만 바뀐다.

모델 이름과 본문 길이를 함께 저장한다. 앵커를 만든 좌표계와 판정하는
좌표계가 다르면 판정이 조용히 엉키는데, 그건 로그만 봐서는 안 보인다.
topic_classifier.load_anchors() 가 이 값을 대조해 다르면 판정을 끈다.

'무관'을 빼는 이유
---------------
'무관'은 의미 범주가 아니라 편집 판단이다. 용인시의회 의장배 태권도대회는
문장으로는 명백한 대회 기사이고, 임베딩은 그렇게 읽는다 (실측: 이웃 5명
만장일치로 '대회'). 쓸 가치의 판단은 is_on_topic() 게이트가 맡는다.

섞어 두면 무관 앵커가 흐려져 '태권도' 열까지 오염된다 (실측: 무관 → 태권도
6건, 태권도 정밀도 53%).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.topic.topic_classifier import (   # noqa: E402
    ANCHORS_PATH, BODY_CHARS, EVENT, GENERAL, MODEL_NAME, ORG, TOPIC_K,
    encode, make_text, vote,
)
from config.settings import QUALITY_DIR       # noqa: E402

LABELS_PATH = QUALITY_DIR / "labels.csv"
KEEP = (ORG, EVENT, GENERAL)


def _pad(s: str, width: int, right: bool = False) -> str:
    """한글을 두 칸으로 세어 열을 맞춘다."""
    w = sum(2 if ord(ch) > 0x1100 else 1 for ch in s)
    fill = " " * max(width - w, 0)
    return fill + s if right else s + fill


def load_labeled() -> list[dict]:
    if not LABELS_PATH.exists():
        print(f"라벨 파일이 없습니다: {LABELS_PATH}", file=sys.stderr)
        raise SystemExit(1)
    with LABELS_PATH.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("label") or "").strip() in KEEP]
    for r in rows:
        r["label"] = r["label"].strip()
    if not rows:
        print("쓸 수 있는 라벨이 없습니다 (조직·대회·태권도).", file=sys.stderr)
        raise SystemExit(1)
    return rows


def score(X, labels: list[str]) -> None:
    """남겨두기 채점 — 이 앵커가 실제로 몇 점인지 저장 전에 보여준다."""
    preds = [v for v, _s, _m in vote(X, X, labels, loo=True)]
    n = len(labels)
    hit = sum(t == g for t, g in zip(labels, preds))
    print(f"\n  라벨 {n}건 · 3분류 정확도 {hit / n:.1%} ({hit}/{n})")

    print("\n  혼동표 (행=정답, 열=판정)")
    print("    " + _pad("", 9) + "".join(_pad(c, 8, True) for c in KEEP))
    for t in KEEP:
        cells = "".join(
            f"{sum(1 for a, b in zip(labels, preds) if a == t and b == g):>8}" for g in KEEP
        )
        print("    " + _pad(t, 9) + cells)

    # 조직 축만 따로 — 파이프라인이 실제로 쓰는 판단이다.
    # 대회/태권도는 판정기가 가르지 않으므로 한 덩어리로 묶어 본다.
    ok = sum(
        1 for t, g in zip(labels, preds) if (t == ORG) == (g == ORG)
    )
    tp = sum(1 for t, g in zip(labels, preds) if t == ORG and g == ORG)
    pp = preds.count(ORG)
    ap = labels.count(ORG)
    print(f"\n  조직 축 (파이프라인이 쓰는 판단)  정확도 {ok / n:.1%}")
    print(f"    정밀도 {tp / pp if pp else 0:.0%} · 재현율 {tp / ap if ap else 0:.0%} "
          f"· 조직 라벨 {ap}건")


def main() -> None:
    p = argparse.ArgumentParser(description="labels.csv 로 주제 판정 앵커를 만든다")
    p.add_argument("--dry-run", action="store_true", help="저장하지 않고 성능만")
    args = p.parse_args()

    rows = load_labeled()
    labels = [r["label"] for r in rows]
    print(f"라벨 {len(rows)}건 "
          + " · ".join(f"{c} {labels.count(c)}" for c in KEEP))
    print(f"모델 {MODEL_NAME} · 본문 {BODY_CHARS}자 · k={TOPIC_K}")

    X = encode([make_text(r.get("title", ""), r.get("body_clean", "")) for r in rows])
    score(X, labels)

    if args.dry_run:
        print("\n(--dry-run: 저장하지 않았습니다)")
        return

    import numpy as np

    ANCHORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        ANCHORS_PATH,
        vectors=X,
        labels=np.array(labels),
        model=MODEL_NAME,
        body_chars=BODY_CHARS,
    )
    size = ANCHORS_PATH.stat().st_size / 1024
    print(f"\n저장: {ANCHORS_PATH} ({size:.0f}KB)")
    print("  이 파일은 git 에 커밋하세요 — Actions 에서도 같은 앵커를 써야 합니다")


if __name__ == "__main__":
    main()
"""CLIP 으로 CSV 의 '판정' 칸을 자동으로 채운다.

왜 필요한가
----------
300장을 사람이 다 열어보는 것은 현실적이지 않다. 그런데 배제 사유
1~3번(배경·복장·장비)은 대부분 한눈에 갈리는 것들이라, CLIP 이
판단할 수 있는 종류의 문제다.

    "농구 코트인가"  "요가 스튜디오인가"  "축구 유니폼인가"

이런 것은 CLIP 이 잘한다. 반면 "태권도인가 가라테인가"는 못한다.
도복이 비슷하고 자세도 비슷해서 벡터가 거의 겹친다. 그건 사람이
신호별로 채점해야 한다.

그래서 역할을 나눈다.

    CLIP  →  명백히 다른 종목·배경을 x 로 찍는다
    사람  →  남은 것만 본다

무엇을 자동으로 찍나
------------------
아래 라벨 중 하나가 최고점이고 확률이 기준치를 넘으면 x 로 표시한다.
확신이 없으면 건드리지 않고 사람에게 넘긴다. 놓치는 것(false negative)
보다 잘못 버리는 것(false positive)이 비싸므로 기준을 높게 잡았다.

한계
----
CLIP 판정은 완전하지 않다. --apply 로 CSV 를 채운 뒤에도
data/stock_rejected 를 한 번 훑어보는 편이 안전하다. 특히
배제율이 높게 나온 카테고리는 라벨이 과하게 걸린 것일 수 있다.

쓰는 법
------
    python scripts/prefilter_stock.py                    # 판정만 해보고 보고
    python scripts/prefilter_stock.py --apply            # CSV 에 기록
    python scripts/prefilter_stock.py --threshold 0.5    # 기준치 조정
    python scripts/prefilter_stock.py --show-all         # 전 항목 점수 출력
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

MODEL_ID = "openai/clip-vit-large-patch14"

# ── 라벨 ────────────────────────────────────────────────────────────
#
# (문장, 판정, 사유) 세 값이다. 판정이 "x" 면 배제 후보.
#
# 긍정 라벨을 함께 넣는 이유: CLIP 은 softmax 로 상대 비교를 하므로
# 비교 대상이 부정 라벨뿐이면 무엇이든 그 중 하나가 최고점이 된다.
# 통과시켜야 할 장면들을 같이 넣어야 판정이 의미를 갖는다.

LABELS: list[tuple[str, str, str]] = [
    # ── 배제: 배경 ──
    ("a photo of a yoga studio with yoga mats and props", "x", "배경"),
    ("a photo of a pilates reformer machine in a studio", "x", "배경"),
    ("a photo of a weight room with barbells and dumbbells", "x", "배경"),
    ("a photo of a gym with weight machines and treadmills", "x", "배경"),
    ("a photo of a basketball court with hoops", "x", "배경"),
    ("a photo of an ice hockey rink", "x", "배경"),
    ("a photo of a boxing ring with ropes", "x", "배경"),
    ("a photo of a tennis court", "x", "배경"),
    ("a photo of a swimming pool", "x", "배경"),
    ("a photo of a soccer field with grass", "x", "배경"),
    ("a photo of an outdoor running track", "x", "배경"),
    ("a photo of a climbing wall", "x", "배경"),

    # ── 배제: 다른 종목 장비 ──
    ("a photo of a person holding a basketball", "x", "종목"),
    ("a photo of a soccer ball", "x", "종목"),
    ("a photo of a tennis racket", "x", "종목"),
    ("a photo of boxing gloves", "x", "종목"),
    ("a photo of a baseball bat and glove", "x", "종목"),
    ("a photo of a bicycle", "x", "종목"),
    ("a photo of a football helmet", "x", "종목"),
    ("a photo of dumbbells and weight plates", "x", "종목"),

    # ── 배제: 도복이 아닌 복장 ──
    ("a photo of a person wearing a sleeveless sports jersey", "x", "복장"),
    ("a photo of a person wearing yoga leggings", "x", "복장"),
    ("a photo of a person wearing a t-shirt and shorts in a gym", "x", "복장"),
    ("a photo of a person wearing a soccer or basketball uniform", "x", "복장"),
    ("a photo of a person wearing a swimsuit", "x", "복장"),
    ("a photo of a person wearing running shoes and athletic wear", "x", "복장"),

    # ── 통과 후보 (비교 기준) ──
    ("a photo of a person in a white martial arts uniform", "", ""),
    ("a photo of a martial arts training hall with a plain floor", "", ""),
    ("a photo of an empty room with wooden floor and mirrors", "", ""),
    ("a close-up photo of a trophy or a medal", "", ""),
    ("a close-up photo of a folded white uniform and a colored belt", "", ""),
    ("a photo of bare feet on a training mat", "", ""),
    ("a photo of a martial arts kick", "", ""),
    ("a photo of children in white uniforms training", "", ""),
    ("a photo of protective headgear and pads", "", ""),
    ("a photo of an award ceremony podium", "", ""),
]


def load_model():
    """CLIP 을 올린다. 무거우므로 실제 판정 직전에만 부른다."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[load] {MODEL_ID} on {device}")
    model = CLIPModel.from_pretrained(MODEL_ID).to(device).eval()
    processor = CLIPProcessor.from_pretrained(MODEL_ID)
    return model, processor, device, torch


def classify(paths: list[Path], batch: int) -> list[tuple[int, float]]:
    """각 이미지의 (최고 라벨 index, 확률) 을 낸다.

    get_text_features / get_image_features 를 쓰지 않는다. transformers
    버전에 따라 텐서가 아니라 모델 출력 객체를 돌려주는 경우가 있어
    (BaseModelOutputWithPooling) 정규화에서 깨진다. 이미지와 텍스트를
    함께 넣어 logits_per_image 를 받는 쪽이 버전에 흔들리지 않고,
    학습된 온도(logit_scale)도 모델이 알아서 적용한다.

    대신 라벨 텍스트를 배치마다 다시 인코딩한다. 라벨이 40개 미만이라
    이미지 인코딩 비용에 비하면 무시할 만하다.
    """
    from PIL import Image

    model, processor, device, torch = load_model()
    texts = [t for t, _, _ in LABELS]

    out: list[tuple[int, float]] = []
    for start in range(0, len(paths), batch):
        chunk = paths[start:start + batch]
        images = []
        for path in chunk:
            try:
                images.append(Image.open(path).convert("RGB"))
            except Exception as exc:
                print(f"  ! 열지 못함 {path.name}: {exc}")
                images.append(Image.new("RGB", (224, 224)))

        with torch.no_grad():
            inputs = processor(text=texts, images=images, return_tensors="pt",
                               padding=True, truncation=True).to(device)
            probs = model(**inputs).logits_per_image.softmax(dim=-1)

        for row in probs:
            idx = int(row.argmax())
            out.append((idx, float(row[idx])))

        done = min(start + batch, len(paths))
        print(f"  {done}/{len(paths)} …", end="\r", flush=True)

    print(" " * 40, end="\r")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="CLIP 자동 사전 판정")
    parser.add_argument("--dir", default="data/stock_pending")
    parser.add_argument("--csv", default="data/stock_verify.csv")
    parser.add_argument("--threshold", type=float, default=0.40,
                        help="이 확률 이상일 때만 x 로 찍는다 (기본 0.40)")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--apply", action="store_true", help="CSV 에 기록한다")
    parser.add_argument("--show-all", action="store_true", help="전 항목 점수 출력")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV 가 없습니다: {csv_path}")
        print("먼저 verify_stock.py init 을 돌리세요.")
        return 1

    root = Path(args.dir)
    with csv_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0].keys()) if rows else []

    if "판정" not in fields:
        print("CSV 에 '판정' 열이 없습니다. verify_stock.py 를 최신본으로 바꾸세요.")
        return 1

    # 이미 사람이 채운 행은 건드리지 않는다.
    todo = [(i, r) for i, r in enumerate(rows) if not (r.get("판정") or "").strip()]
    if not todo:
        print("판정이 비어 있는 행이 없습니다.")
        return 0

    paths = [root / r["파일명"] for _, r in todo]
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"파일을 찾지 못했습니다 ({len(missing)}개). 예: {missing[0]}")
        return 1

    print(f"판정할 사진 {len(todo)}장 (이미 채워진 {len(rows) - len(todo)}장은 건너뜀)")
    results = classify(paths, args.batch)

    marked = 0
    by_reason: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_category: dict[str, list[int]] = {}

    for (row_idx, row), (label_idx, prob) in zip(todo, results):
        text, verdict, reason = LABELS[label_idx]
        category = (row.get("카테고리") or "").strip()
        stat = by_category.setdefault(category, [0, 0])
        stat[1] += 1

        hit = verdict == "x" and prob >= args.threshold
        if hit:
            rows[row_idx]["판정"] = "x"
            rows[row_idx]["사유"] = reason
            rows[row_idx]["메모"] = f"CLIP {prob:.2f} {text[:40]}"
            marked += 1
            stat[0] += 1
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_label[text] = by_label.get(text, 0) + 1

        if args.show_all:
            flag = "x" if hit else " "
            print(f"  {flag} {row['파일명']:<34} {prob:.2f}  {text[:46]}")

    print(f"\n자동 배제 {marked}장 / {len(todo)}장 ({marked / len(todo) * 100:.0f}%)")
    print(f"사람이 볼 사진 {len(todo) - marked}장")

    if by_reason:
        print("\n사유별")
        for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason}  {count}")

    if by_category:
        print("\n카테고리별 자동 배제율")
        for category, (bad, total) in sorted(by_category.items(),
                                             key=lambda x: -(x[1][0] / max(x[1][1], 1))):
            rate = bad / total * 100 if total else 0
            print(f"  {category or '(미분류)':<10} {bad:>3}/{total:<3} {rate:>3.0f}% "
                  f"{'█' * round(rate / 10)}")

    if by_label:
        print("\n걸린 라벨 (많이 걸린 것이 과하면 LABELS 에서 빼세요)")
        for text, count in sorted(by_label.items(), key=lambda x: -x[1])[:12]:
            print(f"  {count:>3}  {text}")

    if not args.apply:
        print("\n미리보기입니다. CSV 에 기록하려면 --apply 를 붙이세요.")
        return 0

    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{csv_path} 에 기록했습니다.")
    print(f"남은 {len(todo) - marked}장의 '판정' 칸을 채우세요.")
    print("  CSV 를 '판정' 열로 정렬하면 빈 칸만 모입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
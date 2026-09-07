#!/usr/bin/env python
"""이미지 폴더를 CLIP 벡터로 인덱싱한다.

사람이 가끔 손으로 돌리는 준비 작업이다. 이미지를 추가하거나 지웠을 때만
다시 실행하면 된다. 결과로 index.npz(벡터)와 index.json(메타데이터)이 생기고,
검색할 때는 이 두 파일만 읽는다.

인덱싱하는 김에 진단도 한다:
  - 열리지 않는 파일
  - 해상도가 작아 img2img 에 부적합한 파일
  - 사실상 같은 사진(중복)
  - 어떤 주제가 커버되고 어디가 비는지

    python scripts/build_index.py
    python scripts/build_index.py --dir data/images --out data/index
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

# Pillow 가 읽을 수 있는 형식. Unsplash 는 avif, iStock 은 jpg 로 내려준다.
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif", ".heic", ".tif", ".tiff"}

# 삽화 규격. crop_stock.py 가 모든 사진을 이 크기의 정사각으로 맞춘다.
#
# 원래는 img2img 변형에 필요한 최소 해상도(1024)였다. 스톡을 그대로
# 삽화로 쓰게 되면서 기준의 의미가 바뀌었다. 이제 이 크기가 아니라는
# 것은 화질 문제가 아니라 크롭 단계를 안 거쳤다는 신호다.
TARGET_SIDE = 1000

# 코사인 유사도가 이보다 높으면 사실상 같은 사진으로 본다.
DUP_THRESHOLD = 0.98

# 사진첩에 뭐가 있는지 가늠하는 데 쓰는 문장들.
# CLIP 은 영어로 학습됐으므로 프로브도 영어로 쓴다.
PROBES = [
    "two athletes sparring in a match",
    "a person performing a high kick",
    "a taekwondo poomsae form pose",
    "a close-up of a colored belt",
    "a white uniform dobok",
    "hands or fists in a fighting stance",
    "bare feet on a training mat",
    "an empty training hall interior",
    "a group of students training together",
    "a child practicing martial arts",
    "a trophy or medal ceremony",
    "breaking a wooden board",
    "a referee or judge at a competition",
    "protective gear and headguard",
]


def _features(out, projection):
    """get_image_features / get_text_features 의 반환 형태 차이를 흡수한다.

    transformers 버전에 따라 텐서가 아니라 출력 객체가 오는데, 그 안의
    pooler_output 이 투영 전일 수도 있고 이미 투영된 것일 수도 있다.
    투영의 입출력 차원이 다를 때만(vision 은 1024→768) 차원으로 판단하고,
    같을 때는(text 는 768→768) 구분이 불가능하므로 그대로 쓴다.
    """
    if torch.is_tensor(out):
        return out

    for attr in ("image_embeds", "text_embeds"):
        v = getattr(out, attr, None)
        if torch.is_tensor(v):
            return v

    pooled = out.pooler_output
    if (
        projection.in_features != projection.out_features
        and pooled.shape[-1] == projection.in_features
    ):
        return projection(pooled)
    return pooled


def load_model(model_id: str):
    """CLIP 모델을 올린다. 50장 정도면 CPU 로도 몇 분이면 끝난다."""
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[load] {model_id} on {device}")
    model = CLIPModel.from_pretrained(model_id).to(device).eval()
    processor = CLIPProcessor.from_pretrained(model_id)
    return model, processor, device


def encode_images(paths, model, processor, device, batch_size=8):
    """이미지를 벡터로 바꾼다. 열리지 않는 파일은 건너뛰고 따로 모아 보고한다."""
    vectors, meta, broken = [], [], []

    for start in range(0, len(paths), batch_size):
        images, kept = [], []

        for p in paths[start : start + batch_size]:
            try:
                img = Image.open(p).convert("RGB")
                images.append(img)
                kept.append((p, img.size))
            except Exception as e:
                broken.append((p, f"{type(e).__name__}: {e}"))

        if not images:
            continue

        inputs = processor(images=images, return_tensors="pt").to(device)
        with torch.no_grad():
            feats = _features(model.get_image_features(**inputs), model.visual_projection)
            # 길이를 1로 맞춰 두면 나중에 내적만으로 코사인 유사도가 나온다.
            feats = feats / feats.norm(dim=-1, keepdim=True)

        vectors.append(feats.detach().cpu().numpy())
        for p, (w, h) in kept:
            meta.append({"path": str(p), "width": w, "height": h})

        print(f"  {len(meta)}/{len(paths)} …", end="\r")

    print()
    if not vectors:
        raise SystemExit("읽을 수 있는 이미지가 없습니다. --dir 경로를 확인하세요.")

    return np.vstack(vectors), meta, broken


def encode_texts(texts, model, processor, device):
    """문장을 이미지와 같은 공간의 벡터로 바꾼다."""
    inputs = processor(
        text=texts, return_tensors="pt", padding=True, truncation=True
    ).to(device)
    with torch.no_grad():
        feats = _features(model.get_text_features(**inputs), model.text_projection)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.detach().cpu().numpy()


def report_broken(broken):
    if not broken:
        return
    print(f"\n열리지 않은 파일 {len(broken)}개")
    for p, err in broken:
        print(f"  - {Path(p).name}: {err}")


def report_offspec(meta):
    """규격에서 벗어난 파일을 알린다.

    작은 것과 정사각이 아닌 것을 나눠서 본다. 대처가 다르기 때문이다.
    작으면 원본이 부족한 것이고, 정사각이 아니면 크롭을 안 돌린 것이다.
    """
    small, oblong = [], []
    for m in meta:
        w, h = m["width"], m["height"]
        if min(w, h) < TARGET_SIDE:
            small.append(m)
        elif w != h:
            oblong.append(m)

    def show(items, title, advice):
        if not items:
            return
        print(f"\n{title} {len(items)}개")
        print(f"  {advice}")
        for m in items[:10]:
            print(f"  - {Path(m['path']).name}  {m['width']}x{m['height']}")
        if len(items) > 10:
            print(f"  … 외 {len(items) - 10}개")

    show(small, f"규격보다 작은 파일 (짧은 변 < {TARGET_SIDE}px)",
         "삽화로 못 쓸 정도는 아니지만 다른 사진과 크기가 어긋난다.")
    show(oblong, "정사각이 아닌 파일",
         "crop_stock.py 를 안 거친 것으로 보인다.")


def report_duplicates(vecs, meta):
    sim = vecs @ vecs.T
    np.fill_diagonal(sim, 0)

    pairs = [
        (meta[i]["path"], meta[j]["path"], float(sim[i, j]))
        for i, j in zip(*np.where(sim > DUP_THRESHOLD))
        if i < j
    ]
    if not pairs:
        return
    print(f"\n중복으로 보이는 쌍 {len(pairs)}개")
    for a, b, s in pairs:
        print(f"  - {Path(a).name} ↔ {Path(b).name}  ({s:.3f})")


def report_coverage(vecs, model, processor, device):
    """주제별로 몇 장이 있는지 본다. 빈 칸이 곧 새로 만들어야 할 부분이다."""
    probe_vecs = encode_texts(PROBES, model, processor, device)
    assigned = (vecs @ probe_vecs.T).argmax(axis=1)

    print("\n주제별 분포")
    print("-" * 60)
    for k, probe in enumerate(PROBES):
        count = int((assigned == k).sum())
        print(f"  {count:3d}  {'█' * count:<14} {probe}")

    empty = [PROBES[k] for k in range(len(PROBES)) if not (assigned == k).any()]
    if empty:
        print("\n비어 있는 주제 (생성으로 채워야 할 부분):")
        for e in empty:
            print(f"  - {e}")


def main():
    ap = argparse.ArgumentParser(description="이미지 폴더를 CLIP 벡터로 인덱싱")
    ap.add_argument("--dir", default="data/images", help="이미지 폴더")
    ap.add_argument("--out", default="data/index", help="저장할 이름 (확장자 제외)")
    ap.add_argument("--model", default="openai/clip-vit-large-patch14")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.exists():
        raise SystemExit(f"폴더가 없습니다: {root.resolve()}")

    paths = sorted(p for p in root.rglob("*") if p.suffix.lower() in EXTS)
    if not paths:
        raise SystemExit(f"이미지가 없습니다: {root.resolve()}")
    print(f"[scan] {len(paths)}장 발견")

    model, processor, device = load_model(args.model)
    vecs, meta, broken = encode_images(paths, model, processor, device)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out.with_suffix(".npz"), vectors=vecs)
    out.with_suffix(".json").write_text(
        json.dumps(
            {
                "model": args.model,
                "source_dir": str(root),
                "file_count": len(meta),
                "indexed_at": datetime.now().isoformat(timespec="seconds"),
                "images": meta,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[save] {out.with_suffix('.npz')}  {vecs.shape}")

    report_broken(broken)
    report_offspec(meta)
    report_duplicates(vecs, meta)
    report_coverage(vecs, model, processor, device)


if __name__ == "__main__":
    main()
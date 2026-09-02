#!/usr/bin/env python
"""인덱싱된 이미지에서 문장으로 검색한다.

에이전트에서는 ImageSearcher 를 import 해서 쓰고, 결과를 눈으로 확인할 때는
CLI 로 돌린다.

    python tools/image_search.py "sparring match"
    python tools/image_search.py "sparring match" "belt close-up" "empty dojang"

인자를 여러 개 주면 소제목 배정 모드로 동작한다. 한 사진이 두 소제목에
중복되지 않게 처리하고, 마땅한 사진이 없으면 None 을 돌려준다.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import torch
from transformers import CLIPModel, CLIPProcessor

# 이 점수 아래면 "맞는 사진이 없다"고 보고 생성 쪽으로 넘긴다.
# CLIP 코사인 유사도는 절대값이 작다. 0.25~0.30 이 실무적 경계이고,
# 실제 데이터를 보고 조정해야 한다.
MIN_SCORE = 0.25


def _features(out, projection):
    """반환 형태가 텐서일 수도, 출력 객체일 수도 있는 것을 흡수한다."""
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


class ImageSearcher:
    """인덱스를 한 번 올려두고 여러 번 검색한다.

    모델 로딩이 느리므로 워크플로마다 새로 만들지 말고 한 번만 만들어 재사용한다.
    """

    def __init__(self, index="data/index", device=None):
        base = Path(index)
        npz = base.with_suffix(".npz")
        if not npz.exists():
            raise FileNotFoundError(
                f"인덱스가 없습니다: {npz}\n"
                f"먼저 실행하세요: python scripts/build_index.py --dir data/images"
            )

        self.vectors = np.load(npz)["vectors"]
        info = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
        self.meta = info["images"]
        self.paths = [m["path"] for m in self.meta]
        self.indexed_at = info.get("indexed_at")

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = CLIPModel.from_pretrained(info["model"]).to(self.device).eval()
        self.processor = CLIPProcessor.from_pretrained(info["model"])

        self._warn_if_stale(info)

    def _warn_if_stale(self, info):
        """이미지 폴더가 인덱싱 이후에 바뀌었으면 알린다. 막지는 않는다."""
        src = info.get("source_dir")
        if not src or not Path(src).exists():
            return

        exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif", ".heic", ".tif", ".tiff"}

        current = sum(1 for p in Path(src).rglob("*") if p.suffix.lower() in exts)
        if current != info.get("file_count"):
            print(
                f"[경고] 이미지 수가 다릅니다 "
                f"(인덱스 {info.get('file_count')}장, 현재 {current}장). "
                f"build_index.py 를 다시 실행하세요."
            )

    def _encode(self, texts):
        inputs = self.processor(
            text=texts, return_tensors="pt", padding=True, truncation=True
        ).to(self.device)
        with torch.no_grad():
            feats = _features(
                self.model.get_text_features(**inputs), self.model.text_projection
            )
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.detach().cpu().numpy()

    def search(self, query: str, k: int = 3, exclude: set[str] | None = None):
        """문장 하나로 검색해 [(경로, 점수), …] 를 점수 순으로 돌려준다."""
        scores = self.vectors @ self._encode([query])[0]

        results = []
        for i in np.argsort(-scores):
            path = self.paths[i]
            if exclude and path in exclude:
                continue
            results.append((path, float(scores[i])))
            if len(results) >= k:
                break
        return results

    def assign_batch(
        self,
        queries: list[str],
        exclude: set[str] | None = None,
        min_score: float | None = None,
    ):
        """소제목 여러 개에 사진을 하나씩 배정한다.

        순서대로 처리하면 앞쪽 소제목이 애매한 사진을 먼저 채가서 뒤쪽이
        빈손이 될 수 있다. 그래서 점수가 높은 조합부터 확정한다.

        돌려주는 값은 queries 와 같은 길이의 리스트이고, 각 원소는
        (경로, 점수) 또는 None(마땅한 사진 없음)이다.

        min_score 를 주면 기본 경계(MIN_SCORE) 대신 그 값을 쓴다. 0.0 을
        주면 점수와 무관하게 남은 사진 중 최선을 배정한다. GPU 생성이
        불가능할 때 자리를 비우느니 덜 맞는 사진이라도 채우기 위한 용도다.
        (제외된 사진은 -1 로 눌러 두므로 0.0 에서도 걸러진다)
        """
        threshold = MIN_SCORE if min_score is None else min_score
        sim = self.vectors @ self._encode(queries).T   # (이미지, 소제목)

        used = set(exclude or ())
        for i, p in enumerate(self.paths):
            if p in used:
                sim[i, :] = -1

        assigned: dict[int, tuple[str, float] | None] = {}
        remaining = set(range(len(queries)))

        while remaining:
            best_q, best_i, best_s = None, None, -1.0
            for q in remaining:
                i = int(np.argmax(sim[:, q]))
                if sim[i, q] > best_s:
                    best_q, best_i, best_s = q, i, float(sim[i, q])

            if best_q is None or best_s < threshold:
                for q in remaining:
                    assigned[q] = None
                break

            assigned[best_q] = (self.paths[best_i], best_s)
            sim[best_i, :] = -1          # 이 사진은 다른 소제목에 쓰지 않는다
            remaining.discard(best_q)

        return [assigned[q] for q in range(len(queries))]


def main():
    ap = argparse.ArgumentParser(description="CLIP 으로 이미지 검색")
    ap.add_argument("query", nargs="+", help="검색어. 여러 개면 소제목 배정 모드")
    ap.add_argument("--index", default="data/index")
    ap.add_argument("--k", type=int, default=3)
    args = ap.parse_args()

    searcher = ImageSearcher(args.index)

    if len(args.query) == 1:
        for path, score in searcher.search(args.query[0], k=args.k):
            mark = " " if score >= MIN_SCORE else "!"
            print(f"{mark} {score:.3f}  {Path(path).name}")
        print(f"\n! = {MIN_SCORE} 미만. 생성으로 넘기는 것을 권장")
    else:
        for q, hit in zip(args.query, searcher.assign_batch(args.query)):
            if hit:
                print(f"{hit[1]:.3f}  {Path(hit[0]).name:<36} ← {q}")
            else:
                print(f"  —    {'(마땅한 사진 없음)':<36} ← {q}")


if __name__ == "__main__":
    main()
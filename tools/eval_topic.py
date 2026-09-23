#!/usr/bin/env python
"""주제 판정 채점 — 문장 임베딩이 기사를 네 갈래로 나눌 수 있는지 잰다.

    py tools/eval_topic.py                          기본 설정으로 채점
    py tools/eval_topic.py --errors                 오분류 전체 목록
    py tools/eval_topic.py --text title,both        두 방식 비교
    py tools/eval_topic.py --body 300,600,1200      본문 길이별 비교
    py tools/eval_topic.py --probe data/quality/selected_20260922_160423.csv

왜 필요한가
----------
eval_gate.py 는 '이 기사를 쓸 것인가'(통과/탈락)를 잰다. 이 파일은 다른
질문에 답한다 — '이 기사는 무슨 이야기인가'(조직/대회/태권도/무관).

키워드 개수로는 답이 안 나오는 게 이미 확인됐다.

    나사렛대 장운태 금메달   기여 키워드: 세계태권, 품새선수권, 대회, 태권, 연맹
                            → '연맹'이 있으니 조직 신호. 실제로는 선수 기사다.

단어가 거기 있는 이유를 키워드 계수는 못 본다. 임베딩은 문장을 숫자로
바꾸되 뜻이 가까운 것끼리 가깝게 놓으므로, 단어가 안 겹쳐도 같은 종류의
이야기를 묶는다. 그게 여기서 재려는 것이다.

파이프라인은 건드리지 않는다
-------------------------
labels.csv 만 읽고 끝난다. 숫자가 안 나오면 이 파일을 지우면 그만이고,
잘 나오면 같은 코드를 collect_workflow 로 옮긴다.

채점 방식 — 남겨두기(leave-one-out)
---------------------------------
라벨 138건으로 앵커를 만들고 그 138건을 다시 맞히면 100%가 나온다.
자기가 자기 앵커에 들어가 있기 때문이다. 그래서 한 건씩 빼고 나머지로
앵커를 만들어 그 한 건을 맞힌다. 138번 반복한다.

전체를 학습/평가로 쪼개지 않는 이유: 조직 라벨이 18건뿐이라 반으로
나누면 앵커가 9건이 된다. 그 숫자로는 앵커가 흔들려 모델이 아니라
표본을 재게 된다.

읽는 법
------
    4분류 정확도   조직/대회/태권도/무관을 제대로 갈랐는가  ← 레인 배정용
    통과/탈락      무관만 가려냈는가                       ← eval_gate 와 비교용
    판정 여유      1등과 2등 점수 차. 작을수록 애매한 판정이다

'통과/탈락'이 eval_gate 의 정밀도·재현율과 같은 값을 재므로, 판정기가
게이트를 대체할 수 있는지도 여기서 같이 보인다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import PROCESSED_DIR, QUALITY_DIR    # noqa: E402

LABELS_PATH = QUALITY_DIR / "labels.csv"
CACHE_DIR = QUALITY_DIR / "embed"

ORG = "조직"
EVENT = "대회"
GENERAL = "태권도"
OFF = "무관"
LABELS = (ORG, EVENT, GENERAL, OFF)
POSITIVE = (ORG, EVENT, GENERAL)

# 한국어 문장 임베딩 모델. KorSTS 로 학습돼 '문장 뜻이 비슷한가'를 바로
# 답하도록 만들어진 것이다. 440MB 라 CPU 로도 150건에 몇 초면 끝난다.
#
# BGE-M3 를 안 쓰는 이유: 2.2GB 에 다국어라 한국어 단문에서 이득이 뚜렷하지
# 않다. 먼저 가벼운 쪽으로 되는지 보고, 안 되면 그때 바꿔 비교한다.
DEFAULT_MODEL = "jhgan/ko-sroberta-multitask"

# 임베딩에 넣을 본문 길이. 기사 앞부분에 핵심이 오는 역피라미드 구조라
# 뒤를 잘라도 손해가 작고, 모델 입력 한계가 512 토큰이라 어차피 잘린다.
DEFAULT_BODY = 600


# ── 라벨 읽기 ──────────────────────────────────────────────
def load_rows() -> list[dict]:
    """labels.csv → 라벨이 붙은 행만.

    eval_gate.load_labeled() 를 재사용하지 않는 이유: 그쪽은 Article 로
    바꿔 돌려준다. 여기서는 임베딩할 원문(title, body_clean)만 있으면
    되고, Article 을 거치면 필드가 늘 때마다 같이 고쳐야 한다.
    """
    if not LABELS_PATH.exists():
        print(
            f"라벨 파일이 없습니다: {LABELS_PATH}\n"
            "  py tools/label_corpus.py --llm  으로 먼저 만드세요.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    rows: list[dict] = []
    with LABELS_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("label") or "").strip() in LABELS:
                row["label"] = row["label"].strip()
                rows.append(row)
    if not rows:
        print("라벨이 하나도 없습니다.", file=sys.stderr)
        raise SystemExit(1)
    return rows


def make_text(row: dict, mode: str, body_limit: int) -> str:
    """임베딩에 넣을 문자열.

    제목을 항상 앞에 두는 이유: 기사 주제가 가장 압축된 자리이고, 입력이
    512 토큰에서 잘릴 때 뒤가 아니라 앞이 남아야 한다.
    """
    title = (row.get("title") or "").strip()
    if mode == "title":
        return title
    body = re.sub(r"\s+", " ", (row.get("body_clean") or "")).strip()
    return f"{title}. {body[:body_limit]}".strip()


# ── 임베딩 ────────────────────────────────────────────────
def _slug(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", name)


def _load_cache(model_name: str) -> dict:
    """문장 → 벡터 캐시. 같은 문장을 두 번 계산하지 않는다.

    본문 길이나 text 방식을 바꾸면 문장이 달라지므로 자동으로 새 항목이
    된다. 문장 자체의 해시를 열쇠로 쓰는 이유가 그것이다 — 설정별로
    파일을 나누면 길이만 바꿔도 전부 다시 계산하게 된다.
    """
    import numpy as np

    path = CACHE_DIR / f"{_slug(model_name)}.npz"
    if not path.exists():
        return {}
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


def _save_cache(model_name: str, cache: dict) -> None:
    import numpy as np

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(CACHE_DIR / f"{_slug(model_name)}.npz", **cache)


def embed(texts: list[str], model_name: str) -> "object":
    """문장 목록 → 정규화된 벡터 행렬 (캐시 사용).

    transformers 를 직접 쓰고 sentence-transformers 를 안 쓴다. 의존성을
    하나 더 늘리지 않으려는 것이고, prefilter_stock.py 가 CLIP 에서 이미
    같은 방식(모듈 안에서 늦은 import)을 쓰고 있어 결이 맞는다.

    평균 내기(mean pooling)를 쓰는 이유: 이 모델이 그렇게 학습됐다.
    [CLS] 토큰만 쓰면 학습 때와 다른 자리를 읽는 셈이라 점수가 떨어진다.
    """
    import numpy as np

    cache = _load_cache(model_name)
    keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
    missing = sorted({k: t for k, t in zip(keys, texts) if k not in cache}.items())

    if missing:
        import torch
        from transformers import AutoModel, AutoTokenizer

        print(f"임베딩 {len(missing)}건 계산 ({model_name}) …", file=sys.stderr)
        tok = AutoTokenizer.from_pretrained(model_name)
        model = AutoModel.from_pretrained(model_name)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device).eval()

        for i in range(0, len(missing), 16):
            chunk = missing[i:i + 16]
            enc = tok(
                [t for _, t in chunk],
                padding=True, truncation=True, max_length=512, return_tensors="pt",
            ).to(device)
            with torch.no_grad():
                hidden = model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            vec = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            vec = torch.nn.functional.normalize(vec, dim=1).cpu().numpy()
            for (k, _), v in zip(chunk, vec):
                cache[k] = v.astype("float32")

        _save_cache(model_name, cache)

    return np.vstack([cache[k] for k in keys])


# ── 판정 ──────────────────────────────────────────────────
def centroids(X, labels: list[str]) -> dict:
    """라벨별 평균 벡터(앵커). 정규화해 코사인 비교가 내적이 되게 한다."""
    import numpy as np

    out = {}
    for c in LABELS:
        idx = [i for i, l in enumerate(labels) if l == c]
        if not idx:
            continue
        v = X[idx].mean(axis=0)
        out[c] = v / (np.linalg.norm(v) + 1e-9)
    return out


def predict(X, anchors: dict) -> list[tuple[str, float, float]]:
    """각 기사의 (판정, 1등 점수, 여유). 여유 = 1등 − 2등."""
    import numpy as np

    names = list(anchors)
    A = np.vstack([anchors[c] for c in names])
    scores = X @ A.T
    out = []
    for row in scores:
        order = np.argsort(-row)
        top, second = order[0], order[1] if len(order) > 1 else order[0]
        out.append((names[top], float(row[top]), float(row[top] - row[second])))
    return out


def predict_loo(X, labels: list[str]) -> list[tuple[str, float, float]]:
    """남겨두기 판정 — 자기 자신을 앵커에서 뺀 뒤 맞힌다."""
    import numpy as np

    sums = {c: X[[i for i, l in enumerate(labels) if l == c]].sum(axis=0) for c in LABELS
            if any(l == c for l in labels)}
    counts = {c: labels.count(c) for c in sums}

    out = []
    for v, own in zip(X, labels):
        anchors = {}
        for c, s in sums.items():
            n = counts[c] - (1 if c == own else 0)
            if n <= 0:
                continue
            cen = (s - v) / n if c == own else s / counts[c]
            anchors[c] = cen / (np.linalg.norm(cen) + 1e-9)
        out += predict(v.reshape(1, -1), anchors)
    return out


def predict_knn(X, Xref, labels: list[str], k: int, loo: bool = False):
    """k-최근접 판정 — 가장 닮은 이웃 k건이 투표한다.

    왜 평균 앵커와 따로 두는가
    ------------------------
    평균은 범주가 한 덩어리로 뭉쳐 있을 때만 맞는다. '조직'은 국기원·연맹·
    협회가 뭔가를 했다는 이야기라 뭉쳐 있지만, '태권도'와 '무관'은 "그
    나머지"라서 흩어져 있다 — 도장·유네스코·시범단·해외보급이 한 평균으로
    뭉개지면 그 평균은 어느 기사와도 안 닮은 점이 된다.
    (실측 2026-09-22: 태권도 정밀도 53%, 무관 재현율 47%)

    이웃 투표는 범주가 뭉쳐 있을 필요가 없다. 도장 기사는 다른 도장 기사와만
    닮으면 되고 시범단 기사와 닮을 이유가 없다.

    점수·여유는 득표 '비율'로 낸다. 코사인 차이(평균 앵커)와 눈금이 다르므로
    두 방식의 여유 값을 직접 비교하지 말 것 — 채택률로 보면 비교가 된다.
    """
    import numpy as np

    S = X @ Xref.T
    if loo:
        np.fill_diagonal(S, -2.0)   # 자기 자신은 이웃에서 뺀다

    out = []
    for row in S:
        idx = np.argsort(-row)[:k]
        weight: dict[str, float] = {}
        for j in idx:
            # 음수 유사도는 0으로. 반대 방향인 이웃이 표를 깎으면 k 안에
            # 들어왔다는 사실 자체가 뒤집힌다.
            weight[labels[j]] = weight.get(labels[j], 0.0) + max(float(row[j]), 0.0)
        ranked = sorted(weight.items(), key=lambda kv: -kv[1])
        total = sum(weight.values()) or 1.0
        top = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        out.append((ranked[0][0], top / total, (top - second) / total))
    return out


# ── 보고 ──────────────────────────────────────────────────
def _pad(s: str, width: int, right: bool = False) -> str:
    """한글을 두 칸으로 세어 열을 맞춘다.

    f"{'태권도':<8}" 은 글자 수만 세므로 콘솔에서 열이 어긋난다. 혼동표는
    눈으로 읽는 표라 어긋나면 읽는 사람이 행과 열을 잘못 짝짓는다.
    """
    w = sum(2 if ord(ch) > 0x1100 else 1 for ch in s)
    fill = " " * max(width - w, 0)
    return fill + s if right else s + fill


def report(rows: list[dict], preds: list[tuple[str, float, float]], show_errors: bool) -> dict:
    truth = [r["label"] for r in rows]
    guess = [p[0] for p in preds]
    n = len(rows)
    hit = sum(t == g for t, g in zip(truth, guess))

    # 쓰이지 않는 범주는 표에서 뺀다. --drop-off 로 '무관'을 빼면 그 행과
    # 열이 0 으로만 차서, 읽는 사람이 '전부 틀렸나' 로 잘못 읽는다.
    active = [c for c in LABELS if c in truth or c in guess]
    print(f"\n라벨 {n}건 · {len(active)}분류 정확도 {hit / n:.1%} ({hit}/{n})")

    print("\n  혼동표 (행=정답, 열=판정)")
    print("    " + _pad("", 9) + "".join(_pad(c, 8, right=True) for c in active))
    for t in active:
        cells = "".join(
            f"{sum(1 for a, b in zip(truth, guess) if a == t and b == g):>8}" for g in active
        )
        print("    " + _pad(t, 9) + cells)

    print("\n  카테고리별")
    print("    " + _pad("", 9) + _pad("정밀도", 8, True) + _pad("재현율", 8, True)
          + _pad("정답", 7, True))
    for c in active:
        tp = sum(1 for t, g in zip(truth, guess) if t == c and g == c)
        pp = guess.count(c)
        ap = truth.count(c)
        prec = tp / pp if pp else 0.0
        rec = tp / ap if ap else 0.0
        print("    " + _pad(c, 9) + f"{prec:>8.0%}{rec:>8.0%}{ap:>7}")

    # 통과/탈락 — eval_gate 와 같은 눈금.
    # '무관'이 없으면 전부 통과가 되어 항상 100% 다. 잴 것이 없으므로 뺀다.
    tp = sum(1 for t, g in zip(truth, guess) if t in POSITIVE and g in POSITIVE)
    fp = sum(1 for t, g in zip(truth, guess) if t == OFF and g in POSITIVE)
    fn = sum(1 for t, g in zip(truth, guess) if t in POSITIVE and g == OFF)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    if OFF in truth:
        print(f"\n  통과/탈락  정밀도 {prec:.1%} · 재현율 {rec:.1%} · F1 {f1:.1%} "
              f"(FP {fp} · FN {fn})")
        print("    ↑ eval_gate 의 같은 값과 나란히 놓고 비교할 수 있다")

    # 여유가 큰 것부터 채택할 때의 정확도.
    #
    # 고정 구간(0.02/0.05/…)으로 안 나누는 이유: 평균 앵커의 여유는 코사인
    # 차이고 이웃 투표의 여유는 득표 비율이라 눈금이 다르다. 채택률로 보면
    # 두 방식을 나란히 놓을 수 있고, 무엇보다 파이프라인에서 쓸 값이
    # '몇 %에 개입할 것인가' 이므로 이 모양이 그대로 설계 손잡이가 된다.
    print("\n  여유가 큰 것부터 채택할 때")
    print("    " + _pad("채택률", 8, True) + _pad("여유 기준", 12, True)
          + _pad("건수", 7, True) + _pad("정확도", 9, True))
    # 여유가 같으면 1등 점수로 가른다. 이웃 투표는 k 명이 만장일치면 여유가
    # 1.000 으로 몰려, 여유만으로는 그 안에서 순서가 안 정해진다.
    order = sorted(range(n), key=lambda i: (-preds[i][2], -preds[i][1]))
    for pct in (25, 50, 75, 100):
        take = order[:max(round(n * pct / 100), 1)]
        acc = sum(truth[i] == guess[i] for i in take) / len(take)
        cut = preds[take[-1]][2]
        print("    " + f"{pct:>7}%" + f"{cut:>11.3f}" + f"{len(take):>8}"
              + f"{acc:>9.0%}")

    if show_errors:
        print("\n  오분류")
        for r, (g, s, m), t in zip(rows, preds, truth):
            if g != t:
                print(f"    {t} → {g}  (점수 {s:.2f} · 여유 {m:.2f}) | {r['title'][:52]}")

    return {"acc": hit / n, "prec": prec, "rec": rec, "f1": f1, "fp": fp, "fn": fn}


# ── 실제 수집분에 적용해 보기 ────────────────────────────────
def _resolve_csv(arg: str) -> Path:
    """--probe 인자 → 실제 파일.

    수집분은 data/processed/ 에, 라벨은 data/quality/ 에 있다. 둘 다 이
    도구가 읽는 파일이라 폴더를 헷갈리기 쉽고, 헷갈리면 '파일이 없습니다'
    라는 맞지만 쓸모없는 답이 돌아온다. 파일명만 줘도 찾게 한다.

    빈 값이면 가장 최근 것을 쓴다. 방금 돌린 dry-run 결과를 바로 보는 게
    이 도구의 주된 용도이기 때문이다.
    """
    pool = list(PROCESSED_DIR.glob("selected_*.csv")) + \
           list(PROCESSED_DIR.glob("collected_*.csv"))
    recent = sorted(pool, key=lambda p: -p.stat().st_mtime)

    if not arg:
        if not recent:
            print(f"수집분 CSV가 없습니다: {PROCESSED_DIR}", file=sys.stderr)
            raise SystemExit(1)
        print(f"(최신 파일 사용: {recent[0].name})", file=sys.stderr)
        return recent[0]

    path = Path(arg)
    if path.exists():
        return path
    if (PROCESSED_DIR / path.name).exists():   # 파일명만 준 경우
        return PROCESSED_DIR / path.name

    print(
        f"파일이 없습니다: {arg}\n"
        f"  수집분 CSV는 {PROCESSED_DIR} 에 있습니다. 최근 파일:\n"
        + "\n".join(f"    {q.name}" for q in recent[:5]),
        file=sys.stderr,
    )
    raise SystemExit(1)


def probe(path: Path, rows: list[dict], model: str, mode: str, body: int,
          method: str = "centroid", k: int = 5) -> None:
    """collected_*.csv / selected_*.csv 를 판정해 본다.

    라벨 셋 점수가 좋아도 실제 수집분에서 엉뚱하면 소용없다. 오늘 문제였던
    세 건(나사렛대·용인시의회·창녕소식)이 여기서 제대로 나오는지 본다.
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        targets = [r for r in csv.DictReader(f) if (r.get("title") or "").strip()]

    labels = [r["label"] for r in rows]
    Xref = embed([make_text(r, mode, body) for r in rows], model)
    X = embed([make_text(r, mode, body) for r in targets], model)
    if method == "knn":
        preds = predict_knn(X, Xref, labels, k)
    else:
        preds = predict(X, centroids(Xref, labels))

    # 애매 표시 기준은 이번 판정분의 하위 25% 로 잡는다. 여유 값의 눈금이
    # 판정 방식마다 달라, 고정 숫자를 쓰면 한쪽에서만 맞는 표시가 된다.
    margins = sorted(m for _, _, m in preds)
    weak = margins[max(len(margins) // 4 - 1, 0)] if margins else 0.0

    print(f"\n■ {path.name} — {len(targets)}건 · {method}")
    print("    " + _pad("판정", 7) + _pad("여유", 6, True) + "  "
          + _pad("검색키워드", 16) + "제목")
    for r, (g, s, m) in zip(targets, preds):
        flag = " ?" if m <= weak else "  "
        kw = (r.get("search_keyword") or "")[:12]
        print("    " + _pad(g, 7) + f"{m:>6.2f}" + flag + _pad(kw, 16) + r["title"][:46])
    print("\n    ? = 1·2등 차이가 아래쪽 4분의 1. 판정이 갈린 기사다")


# ── 진입점 ────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="임베딩 주제 판정을 라벨 셋으로 채점한다")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--text", default="both",
                   help="title | both (쉼표로 여러 개: title,both)")
    p.add_argument("--body", default=str(DEFAULT_BODY),
                   help="본문 글자 수 (쉼표로 여러 개: 300,600,1200)")
    p.add_argument("--method", default="centroid",
                   help="centroid | knn (쉼표로 여러 개: centroid,knn)")
    p.add_argument("--k", default="5",
                   help="knn 이웃 수 (쉼표로 여러 개: 3,5,9)")
    p.add_argument("--drop-off", action="store_true",
                   help="'무관' 라벨을 앵커·채점에서 뺀다 (3분류)")
    p.add_argument("--errors", action="store_true", help="오분류 전체 목록")
    p.add_argument("--probe", nargs="?", const="", metavar="CSV",
                   help="수집분 CSV를 판정해 본다 (생략하면 최신 selected_*)")
    args = p.parse_args()

    rows = load_rows()
    if args.drop_off:
        # '무관'은 의미 범주가 아니라 편집 판단이다. 용인시의회 의장배
        # 태권도대회는 문장으로는 명백한 대회 기사이고, 임베딩은 그렇게
        # 읽는다 (실측 2026-09-22: 이웃 5명 만장일치로 '대회').
        #
        # '쓸 가치가 있는가'는 게이트와 홍보성 규칙이 맡고, 판정기는
        # '무엇에 관한 이야기인가'만 정한다. 섞어 두면 무관 앵커가 흐려져
        # 태권도 열까지 오염된다 (무관 → 태권도 6건).
        rows = [r for r in rows if r["label"] != OFF]
        print(f"3분류 모드 — '무관' 제외, 라벨 {len(rows)}건")
    labels = [r["label"] for r in rows]
    modes = [m.strip() for m in args.text.split(",") if m.strip()]
    bodies = [int(b) for b in args.body.split(",") if b.strip()]
    methods = [m.strip() for m in args.method.split(",") if m.strip()]
    ks = [int(v) for v in args.k.split(",") if v.strip()]

    # 빈 문자열(--probe 를 값 없이 쓴 경우)도 실행해야 하므로 is not None.
    if args.probe is not None:
        probe(_resolve_csv(args.probe), rows, args.model, modes[0], bodies[0],
              methods[0], ks[0])
        return

    # title 은 본문을 안 쓰므로 body 값마다 돌릴 이유가 없다 (같은 결과).
    # knn 만 k 를 쓰고 centroid 는 안 쓰므로, 조합에서 k 를 하나로 눌러 둔다.
    combos = [
        (m, b, meth, k)
        for m in modes
        for b in bodies if not (m == "title" and b != bodies[0])
        for meth in methods
        for k in (ks if meth == "knn" else ks[:1])
    ]
    for mode, body, method, k in combos:
        name = method + (f"(k={k})" if method == "knn" else "")
        label = f"{name} · text={mode}" + (f" · body={body}" if mode != "title" else "")
        print(f"\n{'=' * 62}\n{label} · {args.model}\n{'=' * 62}")
        X = embed([make_text(r, mode, body) for r in rows], args.model)
        preds = (predict_knn(X, X, labels, k, loo=True) if method == "knn"
                 else predict_loo(X, labels))
        report(rows, preds, args.errors)


if __name__ == "__main__":
    main()
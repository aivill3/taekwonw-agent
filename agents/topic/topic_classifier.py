"""주제 판정 — 기사가 무엇에 관한 이야기인지 문장 임베딩으로 가른다.

    from agents.topic.topic_classifier import classify

    verdicts = classify(articles)     # ['조직', '대회', '태권도', ...]

왜 필요한가
----------
키워드는 '그 단어가 있느냐'만 본다. 단어가 왜 거기 있는지는 못 본다.

    "세계태권도연맹이 주최한 대회에서 나사렛대 장운태가 금메달"
                     ↑ 키워드가 걸리는 자리
                                  ↑ 기사가 다루는 것

그래서 검색 키워드로 레인(= 발행 카테고리)을 정하면 어긋난다.
(실측 2026-09-22, 수집 143건)

    국기원         13건 중 10건이 조직 기사   77%
    세계태권도연맹  64건 중 14건              22%
    대한태권도협회  30건 중  4건              13%

뒤의 두 키워드는 조직이 '주최자'로만 언급된다. 키워드를 바꿔도 조직명이
기사에 나오기만 하면 걸리므로, 수집 키워드와 발행 카테고리를 분리하는
것 말고는 방법이 없다. 이 모듈이 후자를 맡는다.

무엇을 맡고 무엇을 안 맡는가
-------------------------
    조직이냐 아니냐        맡는다    라벨 116건 채점 96.6%
    대회냐 태권도냐        안 맡는다  79% — 두 범주가 실제로 겹친다
    쓸 가치가 있느냐       안 맡는다  is_on_topic() 게이트의 일

'태권도'는 "조직도 대회도 아닌 나머지"라 의미가 뭉쳐 있지 않다. 도장·
시범단·해외보급 기사가 대회를 언급하면 대회 쪽으로 끌려간다. 라벨 셋에서
오분류 24건 중 15건이 이 한 칸(태권도 → 대회)이었고, 어제 LLM과 사람의
일치율이 갈린 자리도 같았다. 모델을 바꿔도 안 넘는 선이다.

판정 여유(1·2등 차이)로 거르지 않는 이유
----------------------------------
처음엔 '확신할 때만 개입' 규칙을 쓰려 했으나 실측이 반대였다.

    여유 0.02~0.04   WT본부 착공식 기사 4건   전부 조직 정답
    여유 0.41~0.49   일화 마라톤 · AG 코리아하우스 3건   전부 오판

착공식 기사의 여유가 낮은 건 판정이 흔들려서가 아니라, 한 기사에 착공식
(조직)과 대회 개막이 같이 들어 있어 1·2등이 붙기 때문이다. 여유로 거르면
정확한 기사를 버리고 오판을 남긴다.

k-최근접을 쓰는 이유
-----------------
평균 앵커(centroid)는 범주가 한 덩어리로 뭉쳐 있을 때만 맞는다. 라벨
셋에서 평균 71.9% / 이웃 투표 75.3% 였고, 조직 재현율은 72% 대 89% 로
차이가 더 컸다. k 는 5 가 최적이었다 — 9 이상이면 라벨 18건뿐인 조직의
이웃 절반이 다른 범주에서 와 재현율이 67% 로 무너진다.

의존성이 없으면 조용히 꺼진다
--------------------------
torch·transformers 가 없거나 앵커 파일이 없으면 빈 목록을 돌려준다.
호출부는 그때 지금까지의 키워드 배정을 그대로 쓴다. 판정기 때문에 발행이
멈추는 것이 판정기가 없는 것보다 나쁘기 때문이다.
"""
from __future__ import annotations

import os
from pathlib import Path

from config.settings import QUALITY_DIR
from core.logger import get_logger

log = get_logger(__name__)

ANCHORS_PATH = QUALITY_DIR / "anchors.npz"

ORG = "조직"
EVENT = "대회"
GENERAL = "태권도"

# 앵커를 만들 때와 판정할 때가 같아야 한다. 하나라도 다르면 좌표계가
# 달라져 판정이 조용히 엉킨다 — build_anchors.py 가 이 값들을 파일에
# 함께 저장하고, load 시점에 대조한다.
MODEL_NAME = os.getenv("TOPIC_MODEL", "jhgan/ko-sroberta-multitask")
BODY_CHARS = int(os.getenv("TOPIC_BODY_CHARS", "600"))
TOPIC_K = int(os.getenv("TOPIC_K", "5"))

_anchors: tuple | None = None
_tried = False


def make_text(title: str, body: str) -> str:
    """임베딩에 넣을 문자열.

    제목을 항상 앞에 둔다. 기사 주제가 가장 압축된 자리이고, 입력이
    512 토큰에서 잘릴 때 뒤가 아니라 앞이 남아야 한다.
    """
    import re

    title = (title or "").strip()
    body = re.sub(r"\s+", " ", body or "").strip()
    return f"{title}. {body[:BODY_CHARS]}".strip()


def encode(texts: list[str]):
    """문장 목록 → 정규화된 벡터 행렬.

    sentence-transformers 를 안 쓰고 transformers 를 직접 부른다.
    의존성을 하나 덜 늘리려는 것이고, prefilter_stock.py 의 CLIP 이 이미
    같은 방식(함수 안에서 늦은 import)을 쓰고 있어 결이 맞는다.

    평균 내기(mean pooling)를 쓰는 이유: 이 모델이 그렇게 학습됐다.
    [CLS] 토큰만 읽으면 학습 때와 다른 자리를 보는 셈이라 점수가 떨어진다.
    """
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModel.from_pretrained(MODEL_NAME)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device).eval()

    out = []
    for i in range(0, len(texts), 16):
        enc = tok(
            texts[i:i + 16],
            padding=True, truncation=True, max_length=512, return_tensors="pt",
        ).to(device)
        with torch.no_grad():
            hidden = model(**enc).last_hidden_state
        mask = enc["attention_mask"].unsqueeze(-1).float()
        vec = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        out.append(torch.nn.functional.normalize(vec, dim=1).cpu().numpy())
    return np.vstack(out).astype("float32")


def vote(X, Xref, labels: list[str], k: int = TOPIC_K, loo: bool = False):
    """k-최근접 판정 — 가장 닮은 이웃 k건이 투표한다. (판정, 점수, 여유)

    점수·여유는 득표 '비율'이다. loo=True 면 자기 자신을 이웃에서 뺀다
    (채점용 — 자기가 자기 앵커에 들어가 있으면 100% 가 나온다).
    """
    import numpy as np

    S = X @ Xref.T
    if loo:
        np.fill_diagonal(S, -2.0)

    out = []
    for row in S:
        weight: dict[str, float] = {}
        for j in np.argsort(-row)[:k]:
            # 음수 유사도는 0으로. 반대 방향인 이웃이 표를 깎으면 k 안에
            # 들어왔다는 사실 자체가 뒤집힌다.
            weight[labels[j]] = weight.get(labels[j], 0.0) + max(float(row[j]), 0.0)
        ranked = sorted(weight.items(), key=lambda kv: -kv[1])
        total = sum(weight.values()) or 1.0
        top = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        out.append((ranked[0][0], top / total, (top - second) / total))
    return out


def load_anchors():
    """anchors.npz → (벡터, 라벨). 없거나 설정이 어긋나면 None.

    앵커를 매 실행 labels.csv 에서 만들지 않는 이유: 라벨을 한 건 고쳤을
    뿐인데 발행 결과가 달라지면 원인을 찾기 어렵다. 앵커는 명시적으로
    다시 만들 때만 바뀌어야 한다 (py tools/build_anchors.py).
    """
    global _anchors, _tried
    if _tried:
        return _anchors
    _tried = True

    if not ANCHORS_PATH.exists():
        log.warning(
            f"주제 판정 앵커가 없습니다: {ANCHORS_PATH.name} — 키워드 배정을 그대로 씁니다. "
            "py tools/build_anchors.py 로 만드세요"
        )
        return None

    try:
        import numpy as np

        with np.load(ANCHORS_PATH, allow_pickle=False) as z:
            X = z["vectors"]
            labels = [str(s) for s in z["labels"]]
            model = str(z["model"])
            body = int(z["body_chars"])
    except Exception as e:                      # noqa: BLE001
        log.warning(f"앵커를 읽지 못했습니다 ({e}) — 키워드 배정을 그대로 씁니다")
        return None

    if model != MODEL_NAME or body != BODY_CHARS:
        log.warning(
            f"앵커 설정이 다릅니다 (앵커 {model}/{body} · 지금 {MODEL_NAME}/{BODY_CHARS}) "
            "— 키워드 배정을 그대로 씁니다. build_anchors.py 를 다시 도세요"
        )
        return None

    _anchors = (X, labels)
    log.info(f"주제 판정 앵커 {len(labels)}건 · k={TOPIC_K} · {model}")
    return _anchors


def classify(articles) -> list[str]:
    """기사 목록 → 판정 라벨 목록. 판정할 수 없으면 빈 목록.

    빈 목록을 돌려주는 경우: 앵커 없음 · 설정 불일치 · torch 미설치 ·
    모델 내려받기 실패. 호출부는 길이가 0 이면 키워드 배정을 그대로 쓴다.
    """
    if not articles:
        return []

    loaded = load_anchors()
    if loaded is None:
        return []
    Xref, labels = loaded

    try:
        X = encode([make_text(a.title, a.body_clean) for a in articles])
    except Exception as e:                      # noqa: BLE001
        log.warning(f"주제 판정을 건너뜁니다 ({e}) — 키워드 배정을 그대로 씁니다")
        return []

    return [v for v, _score, _margin in vote(X, Xref, labels)]
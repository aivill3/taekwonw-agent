"""감성사전 기반 문장 극성 분석.

딥러닝 감성 모델 대신 사전 방식을 쓰는 이유는 재현성이다.
같은 글을 두 번 검사했을 때 결과가 달라지면 발행 게이트로 쓸 수 없다.

기본 사전은 KNU 한국어 감성사전(군산대)을 사용한다.
    https://github.com/park1200656/KnuSentiLex
data/dictionaries/SentiWord_info.json 에 넣어두면 자동으로 읽고,
없으면 내장 fallback 사전으로 동작한다.
"""

from __future__ import annotations

import json
from pathlib import Path

from .context_rules import ContextualPolarityResolver
from agents.quality.quality_models import Polarity, SentenceSentiment, SentimentResult
from core.korean_morphology import LemmaUnit, SentenceUnit, is_negation
from config.settings import DICT_DIR

# 감정 판정 대상 품사. 조사·어미 등은 극성이 없다.
_TARGET_TAGS = {"NNG", "NNP", "VV", "VA", "VX", "VCN", "MAG", "XR", "IC"}

# 최대 n-gram 길이. '기분 좋다' 같은 복합 표현 대응.
_MAX_NGRAM = 3

# 부정어 탐색 범위(뒤쪽 형태소 개수)
_NEGATION_WINDOW = 3


def _dedupe_units(units: list[LemmaUnit]) -> list[LemmaUnit]:
    """겹치는 LemmaUnit을 제거한다.

    '제압하'는 결합형('제압하다')과 명사형('제압')이 모두 생성되는데,
    감정 점수를 두 번 더하면 안 되므로 더 긴 결합형만 남긴다.
    """
    ordered = sorted(units, key=lambda u: (u.start, -(u.end - u.start)))
    result: list[LemmaUnit] = []
    covered_until = -1
    for unit in ordered:
        if unit.start < covered_until:
            continue
        result.append(unit)
        covered_until = unit.end
    return result


class SentimentDictionary:
    """원형 -> 극성 점수(-2 ~ +2) 매핑."""

    def __init__(
        self,
        scores: dict[str, int],
        neutral_overrides: set[str] | None = None,
        functional_rules: dict[str, set[str]] | None = None,
    ) -> None:
        self._neutral = neutral_overrides or set()
        # 도메인 예외는 사전에서 아예 제거해 조회 자체를 막는다.
        self._scores = {w: s for w, s in scores.items() if w not in self._neutral}
        # 선행 조사에 따라 기능어로 처리할 단어. 조회 시점에 판단한다.
        self._functional = functional_rules or {}

    def is_functional(self, lemma: str, prev_tag: str | None) -> bool:
        """선행 조사를 보고 기능어 용법인지 판단한다.

        'A를 통해'(JKO) -> 수단을 나타내는 문법 표현, 감정 없음
        '뜻이 통하다'(JKS) -> 실질 동사, 감정 있음
        """
        tags = self._functional.get(lemma)
        return bool(tags) and prev_tag in tags

    def __len__(self) -> int:
        return len(self._scores)

    @property
    def neutral_overrides(self) -> set[str]:
        return set(self._neutral)

    def get(self, word: str) -> int | None:
        return self._scores.get(word)

    @classmethod
    def load(
        cls,
        dict_dir: Path | None = None,
        domain_neutral_file: str = "domain_neutral.json",
        domain_sentiment_file: str = "domain_sentiment.json",
    ) -> "SentimentDictionary":
        """사전을 3계층으로 쌓아 올린다.

            1) KNU 감성사전 (없으면 내장 폴백)
            2) 도메인 보강 사전 -- KNU를 덮어쓴다
            3) 도메인 중립화 -- 최종적으로 조회 자체를 막는다

        순서가 중요하다. 보강이 KNU를 이기고, 중립화가 모두를 이긴다.
        """
        base = dict_dir or DICT_DIR
        scores: dict[str, int] = {}

        knu_path = base / "SentiWord_info.json"
        if knu_path.exists():
            scores.update(_load_knu(knu_path))
        else:
            fallback = base / "sentiment_fallback.json"
            if fallback.exists():
                raw = json.loads(fallback.read_text(encoding="utf-8"))
                for key, value in raw.items():
                    if key.startswith("_"):  # 주석용 키
                        continue
                    try:
                        scores[key] = int(value)
                    except (TypeError, ValueError):
                        continue

        # 2계층: 도메인 보강. KNU 점수를 덮어쓴다.
        supp_path = base / domain_sentiment_file
        if supp_path.exists():
            data = json.loads(supp_path.read_text(encoding="utf-8"))
            for group in ("positive", "negative"):
                for word, value in data.get(group, {}).items():
                    if word.startswith("_"):  # 주석용 키
                        continue
                    try:
                        scores[word] = int(value)
                    except (TypeError, ValueError):
                        continue

        neutral: set[str] = set()
        functional: dict[str, set[str]] = {}
        neutral_path = base / domain_neutral_file
        if neutral_path.exists():
            data = json.loads(neutral_path.read_text(encoding="utf-8"))
            for group in data.get("neutral_words", {}).values():
                neutral.update(group)
            for word, tags in data.get("functional_when_preceded_by", {}).items():
                if word.startswith("_"):  # 주석용 키
                    continue
                functional[word] = set(tags)

        return cls(scores, neutral, functional)


def _load_knu(path: Path) -> dict[str, int]:
    """KNU 감성사전 JSON을 원형->점수 매핑으로 변환한다.

    원본 엔트리의 word 필드는 '가난 하 다'처럼 형태소가 공백으로 분리돼 있다.
    공백을 제거해 '가난하다' 형태로 정규화해야 Kiwi 원형과 매칭된다.
    """
    scores: dict[str, int] = {}
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data:
        word = str(entry.get("word", "")).replace(" ", "")
        if not word:
            continue
        try:
            polarity = int(entry.get("polarity", 0))
        except (TypeError, ValueError):
            continue
        if polarity == 0:
            continue
        # 중복 표제어는 절댓값이 큰 쪽을 채택한다.
        if abs(polarity) >= abs(scores.get(word, 0)):
            scores[word] = polarity
    return scores


def _score_sentence(
    sentence: SentenceUnit,
    dictionary: SentimentDictionary,
    resolver: "ContextualPolarityResolver | None" = None,
) -> tuple[float, list[str]]:
    """문장 1개의 감정 점수와 매칭 단어 목록을 계산한다."""
    units = _dedupe_units(sentence.units)
    total = 0.0
    matched: list[str] = []
    n = len(units)

    # 1차 통과: 증감 동사를 논항 규칙으로 처리한다.
    # 논항으로 쓰인 명사는 consumed에 넣어 2차 통과에서 건너뛴다.
    # '부상자가 줄어들었다'에서 규칙이 +1을 주는데 '부상자' 자체의 -1까지
    # 더하면 0으로 상쇄되기 때문이다.
    consumed: set[int] = set()
    if resolver is not None:
        for i in range(n):
            if not resolver.is_direction_verb(units[i].lemma):
                continue
            resolved = resolver.resolve(units, i, dictionary.get)
            if resolved is None:
                continue
            score, label, arg_index = resolved
            window = units[i + 1 : i + 1 + _NEGATION_WINDOW]
            if any(is_negation(u) for u in window):
                score = -score
                label = f"{label}(부정)"
            total += score
            matched.append(label)
            consumed.add(i)
            if arg_index is not None:
                consumed.add(arg_index)

    # 2차 통과: 나머지를 감성사전으로 채점한다.
    i = 0
    while i < n:
        if i in consumed:
            i += 1
            continue

        hit_len = 0
        hit_word = ""
        hit_score = 0

        # 긴 n-gram부터 시도해 복합 표현을 우선 매칭한다.
        for size in range(min(_MAX_NGRAM, n - i), 0, -1):
            if any(j in consumed for j in range(i, i + size)):
                continue
            chunk = units[i : i + size]
            if size == 1 and chunk[0].tag not in _TARGET_TAGS:
                continue
            candidate = "".join(u.lemma for u in chunk)
            # 기능어 용법이면 감정 점수를 매기지 않는다.
            prev_tag = units[i - 1].tag if i > 0 else None
            if dictionary.is_functional(candidate, prev_tag):
                continue
            score = dictionary.get(candidate)
            if score is not None:
                hit_len, hit_word, hit_score = size, candidate, score
                break

        if hit_len == 0:
            i += 1
            continue

        # 부정 표현이 뒤따르면 극성을 반전한다. ('예쁘지 않다')
        window = units[i + hit_len : i + hit_len + _NEGATION_WINDOW]
        if any(is_negation(u) for u in window):
            hit_score = -hit_score
            hit_word = f"{hit_word}(부정)"

        total += hit_score
        matched.append(f"{hit_word}:{hit_score:+d}")
        i += hit_len

    return total, matched


def analyze_sentiment(
    sentences: list[SentenceUnit],
    dictionary: SentimentDictionary,
    threshold: float = 0.0,
    resolver: ContextualPolarityResolver | None = None,
) -> SentimentResult:
    """문서 전체의 감정 비율을 계산한다.

    문장 점수 합이 threshold를 초과하면 긍정, 미만이면 부정, 그 사이는 중립.
    resolver를 넘기면 증감 동사에 논항 기반 문맥 규칙이 적용된다.
    """
    results: list[SentenceSentiment] = []
    for sent in sentences:
        score, matched = _score_sentence(sent, dictionary, resolver)
        if score > threshold:
            polarity = Polarity.POSITIVE
        elif score < -threshold:
            polarity = Polarity.NEGATIVE
        else:
            polarity = Polarity.NEUTRAL
        results.append(
            SentenceSentiment(
                index=sent.index,
                text=sent.text,
                score=score,
                polarity=polarity,
                matched_words=matched,
            )
        )

    total = len(results) or 1
    pos = sum(1 for r in results if r.polarity is Polarity.POSITIVE)
    neg = sum(1 for r in results if r.polarity is Polarity.NEGATIVE)
    neu = len(results) - pos - neg

    return SentimentResult(
        positive_ratio=round(pos / total, 4),
        negative_ratio=round(neg / total, 4),
        neutral_ratio=round(neu / total, 4),
        total_sentences=len(results),
        average_score=round(sum(r.score for r in results) / total, 4),
        sentences=results,
    )

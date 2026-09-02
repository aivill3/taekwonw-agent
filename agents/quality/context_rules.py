"""문맥 의존 극성 해석 (논항 기반 규칙).

문제
----
'늘어나다'는 그 자체로 극성이 없다. 무엇이 늘어나는지가 극성을 결정한다.

    참가자가 늘어나다  ->  참가자(+) x 증가(+)  ->  긍정
    피해가   늘어나다  ->  피해(-)   x 증가(+)  ->  부정
    부상자가 줄어들다  ->  부상자(-) x 감소(-)  ->  긍정

그런데 감성사전은 단어 하나에 점수 하나를 고정으로 매긴다. KNU 사전은
'늘어나다'를 -1로 등록해 두었고, 그래서 "참가자가 늘어나다"가 부정으로 잡힌다.

해결
----
증감 동사를 별도 클래스로 분리하고, 사전 조회 대신
**논항(주어/목적어) 명사의 극성 x 동사의 방향 부호**로 점수를 계산한다.

    score = valence(논항 명사) * direction(동사) * INTENSITY

논항 명사의 극성은 세 단계로 조회한다.
    1) valence_nouns.json 의 도메인 사전
    2) 감성사전(KNU)에 등재된 명사 점수
    3) 둘 다 없으면 0 (중립)

3단계가 핵심이다. 모르는 명사면 잘못된 부호를 찍는 대신 중립으로 둔다.
사전 방식의 오탐은 대부분 '확신 없이 부호를 찍어서' 생긴다.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.korean_morphology import LemmaUnit
from config.settings import DICT_DIR

# 논항으로 인정할 조사. 주격/보조사/목적격/보격.
_ARG_JOSA_TAGS = {"JKS", "JX", "JKO", "JKC"}

# 논항 탐색 시 거슬러 올라갈 최대 형태소 수
_ARG_SEARCH_WINDOW = 8

# 명사 극성에 곱할 강도. 사전 점수(-2~+2)와 눈금을 맞춘다.
_INTENSITY = 1


class ContextualPolarityResolver:
    """증감 동사의 극성을 논항 명사로부터 유도한다."""

    def __init__(
        self,
        directions: dict[str, int],
        noun_valence: dict[str, int],
    ) -> None:
        self._directions = directions  # 원형 -> +1(증가) / -1(감소)
        self._noun_valence = noun_valence  # 명사 -> +1 / -1

    @classmethod
    def load(cls, dict_dir: Path | None = None) -> "ContextualPolarityResolver":
        base = dict_dir or DICT_DIR

        directions: dict[str, int] = {}
        path = base / "direction_verbs.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for word in data.get("increase", []):
                directions[word] = 1
            for word in data.get("decrease", []):
                directions[word] = -1

        valence: dict[str, int] = {}
        path = base / "valence_nouns.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for word in data.get("desirable", []):
                valence[word] = 1
            for word in data.get("undesirable", []):
                valence[word] = -1

        return cls(directions, valence)

    @property
    def direction_verbs(self) -> set[str]:
        return set(self._directions)

    def is_direction_verb(self, lemma: str) -> bool:
        return lemma in self._directions

    def _find_argument(
        self,
        units: list[LemmaUnit],
        verb_index: int,
    ) -> int | None:
        """동사 앞쪽에서 조사가 붙은 가장 가까운 명사의 인덱스를 찾는다.

        '참가자 + 가(JKS) + ... + 늘어나다' 구조에서 '참가자'를 집어낸다.
        조사를 요구하는 이유는 '크게' 같은 부사나 관형 수식어를 배제하기 위함이다.
        """
        start = max(0, verb_index - _ARG_SEARCH_WINDOW)
        for i in range(verb_index - 1, start - 1, -1):
            unit = units[i]
            if unit.tag not in {"NNG", "NNP"}:
                continue
            # 바로 뒤에 논항 조사가 붙어 있어야 주어/목적어로 인정한다.
            if i + 1 < len(units) and units[i + 1].tag in _ARG_JOSA_TAGS:
                return i
        return None

    def resolve(
        self,
        units: list[LemmaUnit],
        verb_index: int,
        sentiment_lookup,
    ) -> tuple[int, str, int | None] | None:
        """증감 동사의 문맥 점수를 계산한다.

        반환값이 None이면 이 규칙이 적용되지 않는다는 뜻이므로
        호출부는 평소대로 감성사전 조회로 넘어가면 된다.

        세 번째 값은 논항으로 사용된 형태소의 인덱스다. 호출부는 이 위치를
        별도로 채점하지 않아야 한다. '부상자가 줄어들었다'에서 논항 규칙이
        +1을 주는데 '부상자' 자체의 -1까지 더하면 상쇄되어 0이 된다.
        """
        verb = units[verb_index]
        direction = self._directions.get(verb.lemma)
        if direction is None:
            return None

        arg_index = self._find_argument(units, verb_index)
        if arg_index is None:
            # 논항을 못 찾으면 부호를 찍지 않는다.
            return 0, f"{verb.lemma}(논항없음):0", None

        arg = units[arg_index]
        valence = self._noun_valence.get(arg.lemma)
        source = "도메인"
        if valence is None:
            score = sentiment_lookup(arg.lemma)
            if score is not None and score != 0:
                valence = 1 if score > 0 else -1
                source = "감성사전"
        if valence is None:
            return 0, f"{arg.lemma}+{verb.lemma}(극성미상):0", arg_index

        total = valence * direction * _INTENSITY
        arrow = "증가" if direction > 0 else "감소"
        return total, f"{arg.lemma}({source}{valence:+d})+{arrow}:{total:+d}", arg_index

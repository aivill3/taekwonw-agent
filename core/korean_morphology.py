"""한국어 형태소 분석 (kiwipiepy 래퍼).

품질 검사 전반이 형태소에 의존한다. 금칙어는 원형으로 잡아야 활용형을
놓치지 않고('최고다/최고의/최고인'), 감성 분석은 품사를 알아야 조사에
점수를 매기지 않으며, 문체 판정은 문말 어미를 봐야 한다.

Kiwi 인스턴스를 전역 하나로 두는 이유:
  생성에 사전 로딩이 따라와 1~2초가 걸린다. 문장마다 만들면 글 한 편에
  수백 초가 든다. 스레드 안전하므로 하나를 공유한다.
  quality_agent 는 초기화 시점에 get_kiwi() 를 한 번 불러 그 지연을
  첫 검사 요청에서 떼어낸다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import Counter

from core.korean_morphology_types import KeywordStat, MorphologyStats, StyleStat
from core.logger import get_logger

log = get_logger(__name__)

_kiwi = None

# 내용어 품사. 키워드 집계와 어휘 다양도의 분모가 된다.
_CONTENT_TAGS = {"NNG", "NNP", "NNB", "VV", "VA", "VX", "XR", "MAG", "SL", "SN"}
_NOUN_TAGS = {"NNG", "NNP", "NNB"}

# 서술성 접미사. 명사에 붙어 용언을 만든다 (제압 + 하 → 제압하다).
_SUFFIX_VERB_TAGS = {"XSV", "XSA"}

# 용언. 어간에 '다' 를 붙이면 사전 표제어가 된다.
_PREDICATE_TAGS = {"VV", "VA", "VX", "VCN", "VCP"}

# 부정 표현. 앞의 서술어 극성을 뒤집는다.
# '않다/못하다' 는 보조용언(VX), '안/못' 은 부사(MAG), '없다' 는 형용사(VA).
_NEGATION_LEMMAS = {"않다", "아니다", "없다", "못하다", "말다", "안", "못"}
_NEGATION_TAGS = {"VX", "VCN", "MAG", "VA"}

# 문체 판정용 문말 어미. 문장 끝에서만 본다.
_STYLE_PATTERNS = (
    ("해요체", re.compile(r"(?:에요|예요|어요|아요|해요|이에요|세요|네요|죠|거든요|는데요)[.!?…]*$")),
    ("합니다체", re.compile(r"(?:습니다|ㅂ니다|입니다|합니다|십니다|习니까|습니까|ㅂ니까)[.!?…]*$")),
    ("해라체", re.compile(r"(?:다|이다|한다|했다|였다|된다|낸다)[.!?…]*$")),
)

_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass
class LemmaUnit:
    """형태소 하나. 원형·품사와 원문 위치를 함께 들고 다닌다.

    위치를 보존하는 이유: 검사 결과를 사람에게 보고할 때 '어디서 걸렸는지'를
    원문 좌표로 알려줘야 한다. 원형만 남기면 그 정보가 사라진다.
    """

    lemma: str
    tag: str
    start: int
    end: int
    form: str = ""  # 원문 표기 (활용된 형태)


@dataclass
class SentenceUnit:
    """문장 하나와 그 안의 형태소들."""

    index: int
    text: str
    start: int = 0
    end: int = 0
    units: list[LemmaUnit] = field(default_factory=list)


def get_kiwi():
    """전역 Kiwi 인스턴스. 최초 호출에서만 초기화된다."""
    global _kiwi
    if _kiwi is None:
        try:
            from kiwipiepy import Kiwi
        except ImportError as e:
            raise ImportError(
                "kiwipiepy 가 필요합니다. `pip install kiwipiepy` 로 설치하세요."
            ) from e
        log.info("형태소 분석기 초기화 중…")
        _kiwi = Kiwi()
    return _kiwi


def is_negation(unit: LemmaUnit) -> bool:
    """이 형태소가 앞의 서술어를 부정하는가.

    '성과가 없다' 를 긍정으로 세지 않기 위해 필요하다. 감성 분석이 서술어
    뒤 몇 칸을 훑어 이 함수가 참이면 점수 부호를 뒤집는다.
    """
    if unit.lemma in _NEGATION_LEMMAS:
        return True
    base = unit.tag.split("-", 1)[0]
    return base in _NEGATION_TAGS and unit.lemma in _NEGATION_LEMMAS


def _restore_lemmas(tokens, base_offset: int) -> list[LemmaUnit]:
    """Kiwi 토큰열 → LemmaUnit 목록. 원형 복원 3규칙을 적용한다.

    Kiwi 출력을 그대로 쓰면 사전 조회가 자주 실패한다. '우승했어요' 는
    우승/NNG + 하/XSV 로 갈라지는데, 감성사전의 표제어는 '우승하다' 이기 때문이다.
    (실측: 이 복원이 없으면 도메인 감성사전 45개 중 24개가 한 번도 안 걸린다)

        규칙 1    제압/NNG + 하/XSV   → 제압하다   (명사 + 서술성 접미사)
        규칙 1-b  짜증/NNG + 나/VV    → 짜증나다   (명사 + 용언 복합어)
        규칙 2    아쉽/VA-I           → 아쉽다     (용언 어간 + 다)

    규칙 1 에서는 '제압하다' 와 '제압' 을 **둘 다** 만든다. 앞은 감정 분석용,
    뒤는 키워드 통계용이다. 명사를 버리면 '제압' 이 키워드 집계에서 사라진다.

    규칙 1-b 는 접미사가 아니라 독립 용언이 붙는 복합어다. 조건은
    '띄어쓰기 없이 인접' 이다. 그렇지 않으면 '짜증 나서 그만뒀다' 처럼
    실제로 떨어져 쓴 것까지 한 낱말로 묶인다.
    """
    units: list[LemmaUnit] = []
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        tag = tok.tag.split("-", 1)[0]
        start = base_offset + tok.start
        end = start + tok.len

        nxt = tokens[i + 1] if i + 1 < n else None
        merged = False
        if nxt is not None and tag in _NOUN_TAGS:
            nxt_tag = nxt.tag.split("-", 1)[0]
            adjacent = tok.start + tok.len == nxt.start
            # 규칙 1: 명사 + 서술성 접미사 (XSV/XSA). 접미사는 붙여 쓰므로
            #         인접 조건을 따로 걸지 않는다.
            # 규칙 1-b: 명사 + 용언. 붙여 쓴 경우에만 복합어로 본다.
            if nxt_tag in _SUFFIX_VERB_TAGS or (
                nxt_tag in {"VV", "VA"} and adjacent
            ):
                verb = nxt.form if nxt.form.endswith("다") else nxt.form + "다"
                compound_end = base_offset + nxt.start + nxt.len
                units.append(
                    LemmaUnit(
                        lemma=tok.form + verb,
                        tag=f"{tag}+{nxt_tag}",
                        start=start,
                        end=compound_end,
                        form=tok.form + nxt.form,
                    )
                )
                # 명사 단독도 남긴다 (키워드 통계용)
                units.append(
                    LemmaUnit(lemma=tok.form, tag=tag, start=start, end=end,
                              form=tok.form)
                )
                i += 2
                merged = True

        if not merged:
            # 규칙 2: 용언 어간에 '다' 를 붙여 사전 표제어 형태로 만든다
            form = tok.form
            lemma = (
                form + "다"
                if tag in _PREDICATE_TAGS and not form.endswith("다")
                else form
            )
            units.append(
                LemmaUnit(lemma=lemma, tag=tok.tag, start=start, end=end, form=form)
            )
            i += 1

    return units


def _dedupe_units(units: list[LemmaUnit]) -> list[LemmaUnit]:
    """겹치는 구간에서 긴 쪽만 남긴다.

    규칙 1 이 '제압하다'(복합)와 '제압'(명사)을 둘 다 만들기 때문에, 그대로
    두면 같은 글자를 두 번 세게 된다. 감정 점수가 중복 가산되고 토큰 수도
    부풀려진다. 여기서 구간이 겹치면 긴 쪽을 남긴다.

    호출부가 목적에 따라 골라 쓴다:
      감정 분석  → dedupe 적용 (복합어만)
      키워드 통계 → 원본 사용 (명사 포함)
    """
    kept: list[LemmaUnit] = []
    for u in sorted(units, key=lambda x: (x.start, -(x.end - x.start))):
        if kept and u.start < kept[-1].end:
            if (u.end - u.start) > (kept[-1].end - kept[-1].start):
                kept[-1] = u
            continue
        kept.append(u)
    return kept


def analyze(text: str) -> list[SentenceUnit]:
    """본문을 문장 단위로 쪼개고 각 문장을 형태소 분석한다."""
    if not isinstance(text, str) or not text.strip():
        return []

    kiwi = get_kiwi()
    sentences: list[SentenceUnit] = []
    offset = 0
    idx = 0

    for raw in _RE_SENTENCE_SPLIT.split(text):
        chunk = raw.strip()
        if not chunk:
            offset += len(raw) + 1
            continue
        start = text.find(chunk, offset)
        if start < 0:
            start = offset
        end = start + len(chunk)
        offset = end

        units = _restore_lemmas(kiwi.tokenize(chunk), start)
        sentences.append(
            SentenceUnit(index=idx, text=chunk, start=start, end=end, units=units)
        )
        idx += 1

    return sentences


def _detect_style(sentences: list[SentenceUnit]) -> StyleStat:
    """문말 어미로 문체 분포를 센다. 판정 불가 문장은 세지 않는다."""
    counts: Counter[str] = Counter()
    for s in sentences:
        for name, pattern in _STYLE_PATTERNS:
            if pattern.search(s.text.strip()):
                counts[name] += 1
                break

    total = sum(counts.values())
    if not total:
        return StyleStat()
    dominant, top = counts.most_common(1)[0]
    return StyleStat(
        dominant=dominant,
        consistency=top / total,
        distribution=dict(counts),
    )


def summarize(
    sentences: list[SentenceUnit], text: str = "", top_k: int = 15
) -> MorphologyStats:
    """형태소 목록을 글 단위 통계로 접는다.

    어휘 다양도(TTR)는 내용어만으로 계산한다. 조사·어미까지 포함하면 어떤 글이든
    비슷한 값이 나와 '같은 표현을 반복하는가'를 구분하지 못한다.
    """
    content: list[LemmaUnit] = []
    nouns = 0
    for s in sentences:
        for u in s.units:
            base = u.tag.split("-", 1)[0]
            if base in _CONTENT_TAGS:
                content.append(u)
            if base in _NOUN_TAGS:
                nouns += 1

    total = len(content)
    lemmas = [u.lemma for u in content]
    unique = len(set(lemmas))

    counter = Counter(lemmas)
    top_keywords = [
        KeywordStat(word=w, count=n, density=(n / total) if total else 0.0)
        for w, n in counter.most_common(top_k)
    ]

    lengths = [len(s.text) for s in sentences if s.text.strip()]
    avg_len = (sum(lengths) / len(lengths)) if lengths else 0.0

    return MorphologyStats(
        total_tokens=total,
        unique_tokens=unique,
        noun_count=nouns,
        sentence_count=len(sentences),
        avg_sentence_length=avg_len,
        lexical_diversity=(unique / total) if total else 0.0,
        top_keywords=top_keywords,
        style=_detect_style(sentences),
    )
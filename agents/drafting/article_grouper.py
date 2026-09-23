"""수집된 기사를 글 단위로 묶는다.

한 편 = 기사 4건 × 챕터 1개
--------------------------
모든 기사는 소주제 1개를 받고, 기사 4건이 모여 한 편이 된다. 본문 길이는
묶음 여부를 정하지 않는다 (2026-09-17 단독형 폐지).

4건을 못 채우면 발행하지 않고 보류한다. 3건짜리 글을 억지로 2,000자로
늘리면 창작이 들어가기 때문이다. 남은 기사는 다음 수집분과 합친다.

두 가지 묶음 방식
----------------
기존 글을 분석하면 두 패턴이 나온다.

    주제형   한 주제를 여러 측면으로 나눠 쓴다.
             예) '버추얼 태권도' — 종목 채택 / 체험 / 협회 / 메달 구조
             챕터 간 공통 키워드가 뚜렷하다.

    날짜형   그날 나온 소식을 모아 쓴다.
             예) '7월 31일 태권도 소식' — VR / MOU / 칼럼 / 학술대회
             공통 키워드가 '태권도' 하나뿐이다.

주제형이 C-Rank에 유리하다. 한 주제를 깊게 다루는 채널을 높게 평가하기
때문이다. 그래서 주제형을 우선 시도하고 남은 기사만 날짜형으로 묶는다.

묶음 규칙
--------
    1) 주제 유사도로 4건씩 묶음
    2) 그래도 남으면 날짜형으로 4건씩
    3) 4건을 못 채우면 보류

길이는 '재료로 쓸 수 있는가'(MIN_SOURCE_CHARS)만 정하고, 주제 유사도가
'함께 갈 수 있는가'를 정한다.

순서
----
들어온 순서를 지킨다. collect 는 키워드 하나의 기사만, 네이버 검색 순서로 넘긴다.
  - 묶음 안의 기사(챕터) 순서 = 들어온 순서
  - 묶음 사이의 순서 = 각 묶음에서 가장 앞선 기사의 위치
주제형·날짜형을 구분하지 않고 이 위치로만 줄 세운다. 슬롯은 앞 묶음부터 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from agents.drafting.draft_prompt import MIN_SOURCE_CHARS, DraftBrief, SourceArticle
from agents.drafting.corpus_retriever import tokenize

# 한 편의 챕터 수. 기사 4건이 1개씩 채운다.
CHAPTERS_PER_POST = 4

# 묶음 하나의 기사 수. 챕터 수와 같다 (기사 1건 = 챕터 1개).
BUNDLE_SIZE = CHAPTERS_PER_POST

# 묶음 하나의 정보량 권고 상한. 넘어도 4건 규칙을 깨지 않고 경고만 남긴다.
# 4건 고정이 우선이라 여기서 자르면 규칙이 무너진다.
SOFT_BUNDLE_CHARS = 4200

# 주제 판단은 is_same_topic()이 한다. 비율 임계값은 쓰지 않는다.

# '모든 기사에 나오는 단어' 필터를 적용할 최소 기사 수.
# 이보다 적으면 교집합이 곧 주제어라 필터가 역효과를 낸다.
_UNIVERSAL_MIN_DOCS = 4

# 주제 판별에서 제외할 단어. 모든 태권도 기사에 나와 변별력이 없다.
_STOPWORDS = {"태권도", "선수", "대회", "경기", "출전", "참가", "하다", "있다", "되다"}


@dataclass
class Group:
    """한 편의 글이 될 기사 묶음."""

    brief: DraftBrief
    kind: str  # topic | daily | held
    reason: str

    def __len__(self) -> int:
        return len(self.brief)


# ── 발행 구분 (선정 단계에서 노션에 표시) ──────────────
# publish 단계에 가서야 알 수 있던 것을 선정 시점에 미리 알려준다.
# 승인 게이트에서 "이 기사만 승인하면 글이 안 나온다"를 알 수 있어야 하기 때문이다.
MODE_BUNDLE = "묶음"  # 다른 기사 3건과 묶어 한 편이 된다
MODE_HOLD = "보류"    # 재료로 쓰기에도 부족하다


def classify_publish_mode(source_chars: int) -> str:
    """본문 길이로 묶음 재료가 될 수 있는지 판정한다.

    모든 기사는 묶음 재료다. 길이는 '단독으로 갈 수 있는가'를 더 이상 정하지
    않고, 챕터 하나를 채울 내용이 있는지(MIN_SOURCE_CHARS)만 본다.
    """
    if source_chars >= MIN_SOURCE_CHARS:
        return MODE_BUNDLE
    return MODE_HOLD


def _keywords(article: SourceArticle) -> set[str]:
    """주제 비교용 키워드. 제목과 본문에서 뽑는다."""
    text = f"{article.title} {article.content}"
    return {w for w in tokenize(text) if w not in _STOPWORDS and len(w) >= 2}


def _expand(words: set[str]) -> set[str]:
    """복합명사를 부분어로 확장한다.

    Kiwi는 '대한버추얼태권도협회'를 한 덩어리로 잡는다. 그러면
    '버추얼'과 매칭되지 않아 같은 주제인데도 겹침이 0이 된다.
    긴 복합명사에서 다른 키워드가 부분 문자열로 들어 있으면 함께 넣는다.
    """
    out = set(words)
    longs = [w for w in words if len(w) >= 6]
    for long in longs:
        for short in words:
            if 2 <= len(short) < len(long) and short in long:
                out.add(short)
    return out


def _title_keywords(article: SourceArticle) -> set[str]:
    """제목에서만 뽑은 키워드.

    제목은 기사의 주제를 압축한 문장이다. 본문에 스쳐 지나가는 단어와
    달리 제목에 함께 등장하는 단어는 주제가 같다는 강한 신호다.
    """
    title_kws = {
        w for w in tokenize(article.title) if w not in _STOPWORDS and len(w) >= 2
    }
    if not title_kws:
        return set()

    # 제목의 복합명사에서 본문 어휘로 부분어를 꺼낸다.
    # '대한버추얼태권도협회'(제목) + '버추얼'(본문) -> 제목 키워드에 '버추얼' 추가.
    # 이 확장이 없으면 같은 주제인데도 제목 공유가 성립하지 않는다.
    vocab = _keywords(article)
    out = set(title_kws)
    for long in title_kws:
        for short in vocab:
            if 2 <= len(short) < len(long) and short in long:
                out.add(short)
    return out


def shared_topic_words(
    a: SourceArticle,
    b: SourceArticle,
    universal: set[str] | None = None,
) -> set[str]:
    """두 기사가 공유하는 주제어.

    모든 기사에 나오는 단어(universal)는 주제를 구분하지 못하므로 뺀다.
    """
    shared = _expand(_keywords(a)) & _expand(_keywords(b))
    return shared - (universal or set())


def is_same_topic(
    a: SourceArticle,
    b: SourceArticle,
    universal: set[str] | None = None,
) -> bool:
    """같은 주제로 묶을지 판단한다.

    두 조건 중 하나를 만족하면 같은 주제로 본다.

        1) 주제어를 2개 이상 공유          -- 우연의 일치로 보기 어렵다
        2) 주제어 1개를 두 제목에서 공유   -- 제목에 같이 나오면 주제가 같다

    비율(자카드·코사인)을 쓰지 않는 이유는 기사 길이에 따라 값이 크게
    흔들리기 때문이다. 실측에서 같은 주제 쌍이 0.06~0.12, 무관한 쌍이
    0.000으로 나와 임계값을 잡기 어려웠다. 공유 단어의 개수와 위치가
    더 안정적인 신호다.
    """
    shared = shared_topic_words(a, b, universal)
    if not shared:
        return False
    if len(shared) >= 2:
        return True
    ta, tb = _title_keywords(a), _title_keywords(b)
    return bool(shared & ta and shared & tb)


def similarity(a: SourceArticle, b: SourceArticle) -> float:
    """참고용 겹침 비율. 묶음 판단에는 is_same_topic을 쓴다."""
    ka, kb = _expand(_keywords(a)), _expand(_keywords(b))
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / min(len(ka), len(kb))


def _cluster_by_topic(articles: list[SourceArticle]) -> list[list[SourceArticle]]:
    """주제가 겹치는 기사끼리 묶는다.

    단순 연결 요소 탐색이다. A-B가 유사하고 B-C가 유사하면 A,B,C가
    한 묶음이 된다. 기사 수가 하루 수십 건 수준이라 O(n^2)로 충분하다.
    """
    n = len(articles)
    keysets = [_expand(_keywords(a)) for a in articles]

    # 모든 기사에 나오는 단어는 주제를 구분하지 못하므로 뺀다.
    # 다만 기사가 적으면 '전부에 나온다'가 곧 '주제어다'와 같아진다.
    # 2건이 같은 주제면 그 주제어가 교집합 전체가 되어버린다.
    # 그래서 표본이 충분할 때만 이 필터를 적용한다.
    universal = (
        set.intersection(*keysets)
        if len(keysets) >= _UNIVERSAL_MIN_DOCS
        else set()
    )
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if is_same_topic(articles[i], articles[j], universal):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    clusters: dict[int, list[SourceArticle]] = {}
    for i, art in enumerate(articles):
        clusters.setdefault(find(i), []).append(art)
    return list(clusters.values())


def _as_one_chapter(article: SourceArticle) -> SourceArticle:
    """소주제를 1개로 줄인 사본을 만든다.

    새로 생성되는 기사는 소주제가 1개뿐이다. 단독형 시절에 소주제 4개를 받은
    카드가 '선정됨'으로 남아 있으면, 첫 소주제만 써서 챕터 1개로 맞춘다.
    섞인 채로 두면 챕터가 4+1+1+1 = 7개가 되어 4개 고정이 깨진다.
    """
    if len(article.subtopics) <= 1:
        return article
    return replace(article, subtopics=article.subtopics[:1])


def _pack_four(articles: list[SourceArticle]) -> tuple[list[list[SourceArticle]], list[SourceArticle]]:
    """기사를 정확히 4건씩 나눈다.

    반환: (완성된 묶음들, 4건을 못 채우고 남은 기사들)
    남은 기사는 호출자가 다음 단계로 넘기거나 보류시킨다.
    """
    packs: list[list[SourceArticle]] = []
    for i in range(0, len(articles), BUNDLE_SIZE):
        chunk = articles[i : i + BUNDLE_SIZE]
        if len(chunk) == BUNDLE_SIZE:
            packs.append(chunk)
        else:
            return packs, chunk
    return packs, []


def _title_hint(articles: list[SourceArticle], kind: str, day: date | None) -> str:
    if kind == "topic" and articles:
        return articles[0].title
    d = day or date.today()
    stamp = f"{d.month}월 {d.day}일"
    return f"{stamp} 태권도 소식"


def _keyword_hint(articles: list[SourceArticle], kind: str) -> str:
    for a in articles:
        if a.matched_keyword:
            return a.matched_keyword
    if kind == "topic" and articles:
        return articles[0].title
    return "태권도 소식"


def _make_group(
    pack: list[SourceArticle],
    kind: str,
    reason: str,
    day: date | None,
) -> Group:
    brief = DraftBrief(
        articles=pack,
        title_hint=_title_hint(pack, kind, day),
        matched_keyword=_keyword_hint(pack, kind),
    )
    if brief.source_chars > SOFT_BUNDLE_CHARS:
        reason += f" · 원문 {brief.source_chars:,}자로 다소 많음"
    return Group(brief=brief, kind=kind, reason=reason)


def group_articles(
    articles: list[SourceArticle],
    day: date | None = None,
) -> list[Group]:
    """수집된 기사를 4건씩 묶는다. 기사 1건이 챕터 1개를 맡는다.

    반환된 Group 중 kind가 'held'인 것은 발행하지 않고 다음 수집분과
    합쳐야 한다. 지금 쓰면 대부분 창작이 되기 때문이다.
    """
    if not articles:
        return []

    # 모든 기사가 묶음 재료다. 기사 1건이 챕터 1개를 맡는다.
    materials = [_as_one_chapter(a) for a in articles]

    # 들어온 위치. 사본(materials)을 그대로 묶으므로 객체 id 로 찾는다.
    # url 은 비어 있거나 겹칠 수 있어 키로 쓰지 않는다.
    pos = {id(m): i for i, m in enumerate(materials)}

    def first_pos(pack: list[SourceArticle]) -> int:
        return min(pos[id(a)] for a in pack)

    complete: list[tuple[int, Group]] = []

    # ── 1) 주제형 묶음 (4건씩) ────────────────────────
    # 클러스터 안의 기사는 들어온 순서라, 앞선 4건이 먼저 묶인다.
    leftovers: list[SourceArticle] = []
    for cluster in _cluster_by_topic(materials):
        packs, remainder = _pack_four(cluster)
        for pack in packs:
            complete.append((
                first_pos(pack),
                _make_group(
                    pack, "topic",
                    f"주제 묶음 {BUNDLE_SIZE}건 (공통 주제어 발견)", day,
                ),
            ))
        leftovers.extend(remainder)

    # ── 2) 날짜형 묶음 (4건씩) ────────────────────────
    # 주제가 서로 달라도 4건이 모이면 '오늘의 소식'으로 한 편이 된다.
    # 남은 기사는 클러스터 단위로 모였으므로 들어온 순서로 다시 세운다.
    leftovers.sort(key=lambda a: pos[id(a)])
    packs, remainder = _pack_four(leftovers)
    for pack in packs:
        complete.append((
            first_pos(pack),
            _make_group(
                pack, "daily",
                f"날짜 묶음 {BUNDLE_SIZE}건 (주제가 서로 다름)", day,
            ),
        ))

    # 주제형·날짜형 구분 없이 가장 앞선 기사의 위치로 줄 세운다.
    complete.sort(key=lambda pg: pg[0])
    groups: list[Group] = [g for _, g in complete]

    # ── 3) 보류 ───────────────────────────────────────
    # 4건을 못 채운 나머지. 상태를 바꾸지 않고 다음 수집분과 합친다.
    if remainder:
        groups.append(
            Group(
                brief=DraftBrief(
                    articles=remainder,
                    title_hint=_title_hint(remainder, "daily", day),
                    matched_keyword=_keyword_hint(remainder, "daily"),
                ),
                kind="held",
                reason=(
                    f"보류: {len(remainder)}건으로 {BUNDLE_SIZE}건에 "
                    f"{BUNDLE_SIZE - len(remainder)}건 부족. 다음 수집분과 합치세요"
                ),
            )
        )

    return groups


def format_groups(groups: list[Group]) -> str:
    """묶음 결과를 사람이 읽을 요약으로 만든다."""
    if not groups:
        return "묶을 기사가 없습니다."

    label = {"topic": "주제형", "daily": "날짜형", "held": "보류"}
    lines: list[str] = []
    publishable = 0

    for i, g in enumerate(groups, 1):
        lo, hi, ch = g.brief.target_length()
        if g.kind != "held":
            publishable += 1
        lines.append(
            f"[{i}] {label[g.kind]} · {len(g)}건 · {g.brief.source_chars}자 "
            f"-> 목표 {lo:,}~{hi:,}자, 챕터 {ch}개"
        )
        lines.append(f"    {g.reason}")
        for a in g.brief.articles:
            topics = " / ".join(a.subtopics) if a.subtopics else "(소주제 없음)"
            lines.append(f"    - {a.title[:44]} ({a.source_chars}자) → {topics[:60]}")
        lines.append("")

    lines.append(f"발행 가능 {publishable}편 · 보류 {len(groups) - publishable}건")
    return "\n".join(lines).rstrip()
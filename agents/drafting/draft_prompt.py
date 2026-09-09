"""초안 생성 프롬프트 조립.

LLM 호출은 파이프라인 전체에서 이 지점 한 번뿐이다. 검색·조립·검증은
모두 결정론적 코드가 처리하고, LLM은 글쓰기만 담당한다.

프롬프트 구성
------------
    [규칙]   taekwonworld_writing_guide.md 전문 -- 항상, 통째로
    [예시]   과거 글 2~3편 (BM25 검색)      -- 질의에 따라 달라짐
    [소재]   승인된 기사 1건 또는 여러 건    -- 기사 1건 = 챕터 1개
    [지시]   출력 형식 명세

가이드를 통째로 넣는 이유는 규칙서가 부분이 아니라 전체로 의미를 갖기
때문이다. 청크로 쪼개 검색하면 "제목 길이"만 걸리고 "금칙어"는 빠지는
식으로 규칙이 누락된다.

과거 글은 반대다. 수십 편 중 이번 주제와 무관한 글이 대부분이라 전부
넣으면 토큰만 낭비하고 문체 신호가 희석된다. 그래서 검색한다.

검사기와의 관계
--------------
build_prompt()는 프롬프트와 함께 SeoConfig를 돌려준다. 목표 분량과
챕터 수는 소재 정보량에 따라 초안마다 달라지므로, 프롬프트에 지시한
숫자를 그대로 검사 임계값으로 넘겨야 한다. 그러지 않으면 "1,300자로
쓰세요"라고 시킨 뒤 "1,600자 미만입니다"라고 경고하는 모순이 생긴다.

    prompt, hits, seo_cfg = build_prompt(brief, corpus)
    draft = llm(prompt)
    report = checker.check(draft, keyword, seo_config=seo_cfg)
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from pathlib import Path

from config.draft_config import (
    BODY_MAX_CHARS,
    EXAMPLE_EXCERPT_CHARS,
    LENGTH_TIERS,
    MIN_SOURCE_CHARS,
)
from config.quality_config import SeoConfig
from config.settings import GUIDE_DIR
from agents.drafting.corpus_retriever import BlogCorpus, SearchHit

# 분량 기준(LENGTH_TIERS / BODY_MAX_CHARS / MIN_SOURCE_CHARS)과 발췌 길이는
# config/draft_config.py 가 단일 출처다. 여기에 사본을 두지 않는다.
# 사본을 두면 튜닝이 한쪽에만 반영돼, 생성기와 검사기가 다른 값을 본다.

GUIDE_PATH = GUIDE_DIR / "writing_guide.md"

# 챕터별 분량을 따로 지시할 챕터 수의 하한.
#
# 전체 분량만 지시하면 LLM 은 챕터를 "적당히" 쓴다. 챕터가 많을수록
# 그 적당함이 누적돼 전체가 넘친다. 세는 단위를 작게 쓸수록 잘 지킨다.
#
# 실측(2026-08~09, 13편): 단일 기사 글은 평균 1,750자로 범위 안이었으나,
# 묶음 글 3편이 2,000·2,161·2,627자로 상위를 차지했다. 묶음은 챕터마다
# 다른 사건을 다뤄야 해서 담을 내용이 많고, 그만큼 넘치기 쉽다.
CHAPTER_HINT_MIN = 3


@dataclass
class SourceArticle:
    """노션에서 가져온 소재 기사 1건.

    필드명은 노션 데이터베이스 속성과 1:1로 맞춰 두었다.
    """

    title: str  # 제목
    url: str  # URL
    date: str  # 날짜
    source: str  # 출처
    subtopics: list[str] = field(default_factory=list)  # 소주제1~4
    matched_keyword: str = ""  # 매칭키워드
    content: str = ""  # 수집한 기사 본문

    @property
    def source_chars(self) -> int:
        """소재의 실질 정보량. 제목과 소주제도 정보이므로 함께 센다."""
        return (
            len(self.content.strip())
            + len(self.title)
            + sum(len(s) for s in self.subtopics)
        )

    @property
    def is_too_thin(self) -> bool:
        """글을 쓰기에 정보가 부족한가."""
        return self.source_chars < MIN_SOURCE_CHARS

    def target_length(self) -> tuple[int, int, str]:
        """원본 정보량에 맞는 (목표 하한, 목표 상한, 챕터 수)를 고른다."""
        n = self.source_chars
        for threshold, lo, hi, chapters in LENGTH_TIERS:
            if n >= threshold:
                return lo, hi, chapters
        return LENGTH_TIERS[-1][1:]

    @classmethod
    def from_notion_page(cls, page: dict) -> "SourceArticle":
        """노션 API 페이지 객체에서 속성을 뽑는다.

        속성 이름이 바뀌면 여기만 고치면 된다.
        """
        props = page.get("properties", {})

        def plain(name: str) -> str:
            prop = props.get(name, {})
            for key in ("title", "rich_text"):
                items = prop.get(key)
                if items:
                    return "".join(i.get("plain_text", "") for i in items).strip()
            return ""

        subtopics = [
            plain(f"소주제{i}") for i in range(1, 5) if plain(f"소주제{i}")
        ]

        return cls(
            title=plain("제목"),
            url=props.get("URL", {}).get("url") or "",
            date=(props.get("날짜", {}).get("date") or {}).get("start", ""),
            source=plain("출처"),
            subtopics=subtopics,
            matched_keyword=plain("매칭키워드"),
            content=plain("본문") or plain("요약"),
        )

    def as_block(self) -> str:
        lines = [
            f"- 제목: {self.title}",
            f"- 출처: {self.source} ({self.url})",
            f"- 날짜: {self.date}",
        ]
        if self.matched_keyword:
            lines.append(f"- 핵심 키워드: {self.matched_keyword}")
        if self.subtopics:
            lines.append("- 다룰 소주제:")
            lines.extend(f"    {i}. {s}" for i, s in enumerate(self.subtopics, 1))
        if self.content.strip():
            lines.append("- 기사 본문:")
            lines.append(self.content.strip())
        else:
            lines.append(
                "- 기사 본문: (없음) — 제목과 소주제 외의 사실은 쓸 수 없습니다."
            )
        return "\n".join(lines)


@dataclass
class DraftBrief:
    """한 편의 글로 묶을 소재 기사 묶음.

    태권월드 기존 글은 소식 4~5건을 하나의 글로 묶는 구조다.
    단신 하나로 2,000자를 채우려 하면 창작이 발생하므로, 얇은 기사는
    여러 건을 묶어 챕터 하나씩 배정하는 편이 안전하다.

    기사 1건짜리도 이 구조로 다룰 수 있다. from_article() 참고.
    """

    articles: list[SourceArticle] = field(default_factory=list)
    title_hint: str = ""  # 전체 글의 제목 방향. 비우면 LLM이 정한다.
    matched_keyword: str = ""  # 전체 글의 핵심 키워드

    @classmethod
    def from_article(cls, article: SourceArticle) -> "DraftBrief":
        """단일 기사를 묶음으로 감싼다."""
        return cls(
            articles=[article],
            title_hint=article.title,
            matched_keyword=article.matched_keyword,
        )

    @classmethod
    def from_json(cls, path: Path) -> "DraftBrief":
        """JSON 파일에서 묶음을 읽는다.

        {
          "title_hint": "8월 첫째 주 태권도 소식",
          "matched_keyword": "태권도 소식",
          "articles": [
            {"title": "...", "url": "...", "content": "...",
             "subtopics": ["..."], "source": "...", "date": "..."}
          ]
        }
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        articles = [
            SourceArticle(
                title=a.get("title", ""),
                url=a.get("url", ""),
                date=a.get("date", ""),
                source=a.get("source", ""),
                subtopics=list(a.get("subtopics", [])),
                matched_keyword=a.get("matched_keyword", ""),
                content=a.get("content", ""),
            )
            for a in data.get("articles", [])
        ]
        return cls(
            articles=articles,
            title_hint=data.get("title_hint", ""),
            matched_keyword=data.get("matched_keyword", ""),
        )

    def __len__(self) -> int:
        return len(self.articles)

    @property
    def source_chars(self) -> int:
        return sum(a.source_chars for a in self.articles)

    @property
    def is_too_thin(self) -> bool:
        """묶음 전체로 봐도 정보가 부족한가.

        기사 1건이 얇아도 여러 건을 묶으면 충분할 수 있으므로
        개별이 아니라 합계로 판단한다.
        """
        return self.source_chars < MIN_SOURCE_CHARS

    @property
    def thin_articles(self) -> list[SourceArticle]:
        """개별적으로 너무 얇은 기사. 묶어도 챕터를 채우기 어렵다."""
        return [a for a in self.articles if a.source_chars < MIN_SOURCE_CHARS]

    @property
    def chapter_count(self) -> int:
        """만들 챕터 수.

        소주제가 곧 챕터다. 노션의 소주제1~4가 이 역할을 한다.
        기존 글을 보면 소식 4건을 4챕터로 쓴 경우도, 주제 1건을
        4개 측면으로 나눠 4챕터로 쓴 경우도 있다. 둘 다 소주제 수와
        일치하므로 기사 수가 아니라 소주제 수를 센다.

        소주제가 없으면 기사 수로 대신한다.
        """
        total = sum(len(a.subtopics) for a in self.articles)
        return total if total else len(self.articles)

    def target_length(self) -> tuple[int, int, str]:
        """묶음 전체 정보량으로 목표 분량을 정한다."""
        n = self.source_chars
        for threshold, lo, hi, _ in LENGTH_TIERS:
            if n >= threshold:
                return lo, hi, str(self.chapter_count)
        lo, hi, _ = LENGTH_TIERS[-1][1:]
        return lo, hi, str(self.chapter_count)

    def search_query(self) -> str:
        """과거 글 검색용 질의. 모든 기사의 제목과 소주제를 합친다."""
        parts = [self.title_hint, self.matched_keyword]
        for a in self.articles:
            parts.append(a.title)
            parts.extend(a.subtopics)
        return " ".join(p for p in parts if p)

    def as_block(self) -> str:
        """프롬프트의 [소재] 블록을 만든다."""
        if not self.articles:
            return "(소재가 없습니다)"

        lines: list[str] = []
        if self.title_hint:
            lines.append(f"- 글 전체 주제: {self.title_hint}")
        if self.matched_keyword:
            lines.append(f"- 전체 핵심 키워드: {self.matched_keyword}")
        lines.append(
            f"- 다룰 소식 {len(self.articles)}건, 챕터 {self.chapter_count}개"
        )
        lines.append("  각 소주제가 챕터 하나가 됩니다. 소주제 순서를 따르세요.")
        lines.append("")

        for i, a in enumerate(self.articles, 1):
            lines.append(f"[소식 {i}]")
            lines.append(a.as_block())
            lines.append("")
        return "\n".join(lines).rstrip()


def load_guide(path: Path | None = None) -> str:
    """SEO 가이드 전문을 읽는다."""
    p = path or GUIDE_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"SEO 가이드를 찾을 수 없습니다: {p}\n"
            f"이 문서 없이는 초안 품질을 보장할 수 없습니다."
        )
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"SEO 가이드가 비어 있습니다: {p}")
    return text


def build_search_query(article: SourceArticle) -> str:
    """과거 글 검색에 쓸 질의문을 만든다.

    제목과 소주제를 합친다. 소주제에 이번 글에서 실제로 다룰 내용이
    들어 있어 제목만 쓰는 것보다 관련 글을 잘 찾는다.
    """
    parts = [article.title, article.matched_keyword, *article.subtopics]
    return " ".join(p for p in parts if p)


def format_examples(hits: list[SearchHit]) -> str:
    """검색된 과거 글을 예시 블록으로 만든다."""
    if not hits:
        return "(참고할 과거 글이 없습니다. 아래 규칙만 따라 작성하세요.)"

    blocks = []
    for i, hit in enumerate(hits, 1):
        excerpt = hit.text[:EXAMPLE_EXCERPT_CHARS]
        if len(hit.text) > EXAMPLE_EXCERPT_CHARS:
            excerpt += " …"
        blocks.append(f"--- 예시 {i} ---\n{excerpt}")
    return "\n\n".join(blocks)


def build_prompt(
    source: SourceArticle | DraftBrief,
    corpus: BlogCorpus | None = None,
    top_k: int = 3,
    guide_path: Path | None = None,
) -> tuple[str, list[SearchHit], SeoConfig]:
    """초안 생성 프롬프트를 조립한다.

    source는 기사 1건(SourceArticle) 또는 여러 건의 묶음(DraftBrief)이다.
    단건이면 내부에서 묶음으로 감싼다.

    반환값은 (프롬프트, 참고한 과거 글, 이 초안 전용 SeoConfig)이다.
    과거 글 목록은 노션 리포트에 "무엇을 참고했는지" 남기기 위한 것이고,
    SeoConfig는 QualityChecker.check()에 그대로 넘긴다.
    """
    brief = (
        source if isinstance(source, DraftBrief) else DraftBrief.from_article(source)
    )
    article = brief  # 아래 f-string에서 source_chars를 참조한다

    guide = load_guide(guide_path)
    target_lo, target_hi, chapters = brief.target_length()

    # 프롬프트가 지시할 값이 곧 검사기가 강제할 값이다.
    # 분량 하한을 티어에서 가져오는 이유: 소재가 얇을 때 짧게 쓰는 것은
    # 위반이 아니라 올바른 동작이다. 고정 하한(기본 1,600자)을 그대로
    # 적용하면 창작을 유도하지 않으려고 줄인 분량이 매번 경고로 잡힌다.
    # 챕터 수는 소주제 개수와 1:1이므로 고정 하한 대신 실제 개수를 쓴다.
    seo_config = SeoConfig(
        body_min_chars=target_lo,
        # 구간 표를 잘못 고쳐도 2,000자를 넘기지 않는다.
        body_max_chars=min(target_hi, BODY_MAX_CHARS),
        min_chapters=max(1, brief.chapter_count),
    )
    cfg = seo_config

    # 챕터당 목표를 함께 알려 준다.
    #
    # 전체 분량만 주면 챕터마다 "이 정도면 되겠지" 하고 쓰다가 합계가
    # 넘친다. 챕터가 적으면(1~2개) 전체 지시만으로도 감이 잡히므로
    # 굳이 덧붙이지 않는다. 잔소리가 늘면 다른 규칙이 묻힌다.
    n_ch = max(1, brief.chapter_count)
    if n_ch >= CHAPTER_HINT_MIN:
        per_lo, per_hi = target_lo // n_ch, target_hi // n_ch
        per_chapter_hint = (
            f"\n  챕터가 {n_ch}개이므로 **한 챕터는 {per_lo:,}~{per_hi:,}자**입니다."
            f" 한 챕터를 길게 쓰고 다른 챕터를 줄이지 마세요."
        )
    else:
        per_chapter_hint = ""

    hits: list[SearchHit] = []
    if corpus is not None and len(corpus) > 0:
        hits = corpus.search(brief.search_query(), top_k=top_k)

    prompt = f"""당신은 태권도 전문 블로그 '태권월드'의 필자입니다.
아래 [작성 규칙]을 지키고 [문체 예시]의 톤과 구성을 따라
[소재]로 블로그 글 초안을 작성하세요.

────────────────────────────────
[작성 규칙]
────────────────────────────────
{guide}

────────────────────────────────
[문체 예시] 우리 블로그의 기존 글입니다. 문체·구성·분량을 참고하세요.
내용을 베끼지 말고 형식만 따르세요.
────────────────────────────────
{format_examples(hits)}

────────────────────────────────
[소재] 이번에 작성할 글의 재료입니다.
────────────────────────────────
{brief.as_block()}

────────────────────────────────
[출력 형식]
────────────────────────────────
- 네이버 블로그 에디터에 그대로 붙여넣을 형태로 출력합니다.
  마크다운이 아닙니다. `#` 헤딩을 쓰지 마세요.
  줄 첫머리에 `-`, `*`, `•` 를 붙이지 마세요.
- 첫 줄은 제목입니다. `#` 없이 평문으로 씁니다.
  {cfg.title_min}~{cfg.title_max}자로 쓰고,
  핵심 키워드를 앞 {cfg.title_keyword_head}자 안에 둡니다.
- 도입부(첫 두 문단) 안에 핵심 키워드를 한 번 이상 씁니다.
- 챕터는 **'첫 번째는', '두 번째는' 같은 순서 표현으로 시작**합니다.
  {chapters}개 만듭니다. **챕터 줄 앞에 숫자를 붙이지 마세요.**
  숫자는 이미지 자리에만 씁니다.
- **각 덩어리의 첫 줄 끝에 블록 마커를 붙입니다.** 이어지는 줄에는 붙이지 않습니다.
      `/* 소제목 */`  챕터를 여는 문장의 첫 줄
      `/* 본문 */`    일반 문단 덩어리의 첫 줄
      `/* 인용구 */`  실제 발언을 인용하는 덩어리의 첫 줄

      첫 번째는 /* 소제목 */
      대회 개요 이야기예요.

      이번 대회는 /* 본문 */
      32개국이 참가했는데요.

      "준비를 오래 했습니다." /* 인용구 */

  **마커는 덩어리의 첫 줄에만** 붙입니다. 문단마다 빠짐없이 붙이지 마세요.
  한 챕터 안에서 `/* 본문 */` 은 두세 번이면 충분합니다.

  챕터 앞뒤에는 빈 줄을 넣습니다.
- **이미지 자리는 숫자 한 줄로 표시합니다.** `[이미지]` 를 쓰지 마세요.
  1부터 시작해 글 전체에서 이어지는 일련번호이며, 앞뒤로 빈 줄을 2줄 이상 둡니다.
  **챕터 하나당 정확히 하나**, 그 챕터의 내용이 모두 끝난 자리에 넣습니다.
  따라서 총 {chapters}개입니다. 챕터 중간에 끼워 넣지 마세요.

      규모가 역대 최대예요.



      1



      두 번째는 /* 소제목 */

- 표는 소재에 항목-값 대응이 뚜렷한 정보가 있을 때만 만듭니다.
  마크다운 파이프 표 형식을 쓰고, 표에는 마커를 붙이지 않습니다.
- **맨 끝의 태권월드 서비스 안내 문구는 쓰지 않습니다.** 고정 문구이며
  발행 단계에서 그대로 붙습니다. 비슷한 마무리 인사도 만들지 마세요.
- **최상급 조합을 쓰지 마세요. 소재 원문에 있어도 옮기지 않습니다.**
  표시광고법에 걸려 발행이 차단됩니다.
      `국내/세계/전국/업계` + `최초/최고/최대/최상/유일`
      `최고/최상/최강` + `수준/품질/성능/효과/서비스/제품/가격`
      `완벽한`, `무조건`, `100% 보장`, `업계 1위`
  바꿔 쓰는 예:
      국내 최초 개최 -> 국내에서 처음 열리는
      세계 최고 선수들 -> 세계 정상급 선수들
      최고 수준의 경기 -> 수준 높은 경기
      완벽한 호흡 -> 호흡이 잘 맞는
  단독으로 쓴 `최고`는 괜찮습니다 ("예의를 최고 가치로 삼는 무도").
- **한 줄은 {cfg.max_line_chars}자를 넘기지 않습니다.** 의미 단위에서 끊고 줄바꿈합니다.
  접속사(그래서, 그런데, 다만)는 항상 줄 맨 앞에 옵니다.
- 문체는 '해요체'입니다. `~더라고요`, `~거든요`, `~인데요`, `~예요`,
  `~이에요`, `~지요`를 씁니다. '합니다체'를 쓰지 마세요.
  (`~에요`는 표기 오류입니다. 모음 뒤는 `~예요`, 자음 뒤는 `~이에요`입니다.)
- 1인칭으로 씁니다. '저는 태권월드 플랫폼에서 일하면서…'
- 본문은 **{target_lo:,}~{target_hi:,}자**로 작성합니다.{per_chapter_hint}
  줄바꿈과 빈 줄은 이 글자 수에 포함하지 않습니다.
  이 분량은 소재의 정보량({article.source_chars:,}자)에 맞춰 정한 것입니다.
  **분량을 채우려고 같은 말을 반복하거나 내용을 지어내지 마세요.**
  쓸 내용이 부족하면 하한보다 짧아도 괜찮습니다.
- 맨 끝의 태권월드 서비스 안내 문구는 쓰지 않습니다. 발행 단계에서 붙입니다.
- 이미지가 들어갈 자리에는 `[이미지]`만 한 줄로 표시합니다.
- **소재에 없는 사실은 절대 쓰지 마세요.** 이 규칙이 가장 중요합니다.
  아래는 소재에 없으면 쓸 수 없는 내용입니다.
      경기 스코어, 상대 선수, 체급, 라운드 진행 과정
      선수의 훈련 기간·훈련량·성격·각오
      현장 분위기, 관중 반응, 관계자 발언
  정보가 부족해 분량이 모자라면 지어내지 말고,
  태권도 일반 지식이나 도장·학부모 관점의 해석으로 채웁니다.
- 인용문("...")은 소재에 실제 발언이 있을 때만 씁니다.
  없으면 인용 자체를 넣지 않습니다.
- 표(항목/내용 형태)는 소재에 구체적 수치가 있을 때만 만듭니다.
- 설명이나 머리말 없이 본문만 출력합니다.
"""
    return prompt, hits, seo_config
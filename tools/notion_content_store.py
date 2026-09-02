"""Content DB 저장. (승인 단위를 기사에서 '글 한 편'으로 옮기기 위한 저장소)

왜 별도 DB인가
--------------
기존에는 사람이 기사 단위로 승인하고, 승인 뒤에 group_articles() 가 묶었다.
그래서 승인 시점에는 무엇과 무엇이 한 편이 될지 알 수 없었다. 발행구분
'묶음'은 짝이 필요하다는 예고일 뿐 누구와 묶일지는 알려주지 않는다.

Content DB 한 행이 글 한 편이다. 묶기를 승인 앞으로 옮겨, 사람은
"이 기사들을 이 소주제로 한 편을 만드는 것이 타당한가"만 판단한다.

    News DB (기사 원본)  ──Relation──>  Content DB (글 한 편 = 승인 1회)

원문ID 속성을 따로 두는 이유
---------------------------
Relation 은 사람이 클릭해 오갈 용도다. 코드는 원문ID(rich_text)에 담긴
page_id 목록을 읽는다. Relation 속성 생성은 API 스키마가 버전에 민감해
실패할 수 있는데, 그때도 파이프라인은 돌아야 하기 때문이다.
page_id 36자 × 최대 5건 = 200자 남짓이라 2,000자 상한에 여유가 있다.

초안 저장 위치
-------------
Content 페이지 본문에 넣는다. 원문은 토글로 접혀 있으므로 페이지를 열면
초안이 바로 보인다. notion_store.append_illustrated_draft() 를 그대로 쓴다.
"""
from __future__ import annotations

from config.settings import NOTION_CONTENT_DATABASE_ID
from core.logger import get_logger
from agents.drafting.draft_prompt import DraftBrief, SourceArticle
from tools.notion_store import (
    MAX_BLOCKS_PER_REQUEST,
    MAX_TEXT_LEN,
    _merge_select_options,
    _request,
    append_blocks,
    fetch_page_body,
    text_to_blocks,
)

log = get_logger(__name__)

# ── 속성 이름 ──────────────────────────────────────────
PROP_TITLE = "콘텐츠제목"   # title
PROP_STATUS = "상태"        # select
# '승인' checkbox 는 없앴다. 승인은 보드에 카드를 배치하고 confirm 을
# 돌리는 것으로 끝난다. 여기서 한 번 더 체크를 받으면 같은 판단을 두 번
# 하는 셈이라 손만 늘었다. 기존 DB 에 남아 있는 열은 지우지 않는다
# (Notion 에서 직접 삭제하면 된다).
PROP_KIND = "유형"          # select — 주제형 | 날짜형
PROP_REASON = "선정이유"    # rich_text — group_articles() 의 판정 근거
PROP_NEWS = "원문기사"      # relation -> News DB (사람용)
PROP_NEWS_IDS = "원문ID"    # rich_text — page_id 목록 (코드용)
PROP_COUNT = "기사수"       # number
PROP_CHARS = "원문길이"     # number
PROP_TARGET = "목표분량"    # rich_text
PROP_URL = "대표URL"        # url
PROP_WRITER_MODEL = "작성모델"  # rich_text
PROP_MORPH = "형태소"       # rich_text — 품질 측정 요약

# 소주제는 챕터다. article_grouper.MAX_CHAPTERS 와 같은 5개까지 표시한다.
MAX_SUBTOPICS = 5
PROP_SUBTOPICS = [f"소주제{i}" for i in range(1, MAX_SUBTOPICS + 1)]

# ── 상태 ───────────────────────────────────────────────
# News DB 와 같은 이름을 쓴다. 한쪽은 '초안대기', 다른 쪽은 '승인대기'
# 였을 때 같은 시점을 가리키는 두 이름이 있어 헷갈렸다.
#
#   초안대기 — confirm 이 만든 직후. publish 를 기다린다.
#   작성완료 — publish 가 초안을 채웠다.
#   보류     — 발행하지 않기로 함.
STATUS_PENDING = "초안대기"
STATUS_WRITTEN = "작성완료"
STATUS_HOLD = "보류"

# 옛 이름. publish_workflow 등 다른 모듈이 아직 참조할 수 있어 남겨 둔다.
STATUS_WAITING = STATUS_PENDING

# 옵션 개명 이력. ensure_content_schema() 가 옛 이름을 찾으면 id 를 유지한
# 채 이름만 바꾼다. 옵션을 새로 '추가'하면 기존 행 값이 옛 이름에 남는다.
RENAMED_OPTIONS: dict[str, dict[str, str]] = {
    PROP_STATUS: {"승인대기": STATUS_PENDING},
}

KIND_TOPIC = "주제형"
KIND_DAILY = "날짜형"
KIND_LABEL = {"topic": KIND_TOPIC, "daily": KIND_DAILY}

# ── 페이지 본문 구성 ───────────────────────────────────
SUBTOPIC_HEADING = "✍️ 소주제"
SOURCE_HEADING = "📰 원문 기사"

EXPECTED_PROPS: dict[str, dict] = {
    PROP_REASON: {"rich_text": {}},
    PROP_NEWS_IDS: {"rich_text": {}},
    PROP_COUNT: {"number": {"format": "number"}},
    PROP_CHARS: {"number": {"format": "number"}},
    PROP_TARGET: {"rich_text": {}},
    PROP_URL: {"url": {}},
    PROP_WRITER_MODEL: {"rich_text": {}},
    PROP_MORPH: {"rich_text": {}},
    PROP_KIND: {
        "select": {
            "options": [
                {"name": KIND_TOPIC, "color": "green"},
                {"name": KIND_DAILY, "color": "blue"},
            ]
        }
    },
    PROP_STATUS: {
        "select": {
            "options": [
                {"name": STATUS_PENDING, "color": "yellow"},
                {"name": STATUS_WRITTEN, "color": "green"},
                {"name": STATUS_HOLD, "color": "gray"},
            ]
        }
    },
    **{name: {"rich_text": {}} for name in PROP_SUBTOPICS},
}


# 제목 속성 이름 캐시. data_source_id -> 실제 title 속성 이름
_title_prop_cache: dict[str, str] = {}


def _find_title_prop(properties: dict) -> str:
    """title 타입 속성의 '실제 이름'을 찾는다.

    Notion DB 는 title 속성을 하나만 가질 수 있고, 새로 만들 때 이름이
    '이름'(또는 'Name')으로 정해진다. API 로 추가 생성이 불가능하므로
    ensure_content_schema() 도 이 속성만은 만들지 못한다.

    그래서 이름을 가정하지 않고 타입으로 찾는다.
    (실측: '콘텐츠제목' 으로 쓰려다 400 validation_error 발생)
    """
    for name, prop in properties.items():
        if prop.get("type") == "title":
            return name
    return PROP_TITLE


def resolve_title_prop(data_source_id: str) -> str:
    """이 DB 의 제목 속성 이름. 한 번 조회하고 캐시한다."""
    cached = _title_prop_cache.get(data_source_id)
    if cached:
        return cached

    data = _request("GET", f"/data_sources/{data_source_id}")
    name = _find_title_prop(data.get("properties", {}))
    _title_prop_cache[data_source_id] = name
    return name


def resolve_content_data_source_id() -> str:
    """Content DB 의 data source id. News 쪽과 같은 방식이다."""
    if not NOTION_CONTENT_DATABASE_ID:
        raise SystemExit(
            "환경변수 NOTION_CONTENT_DATABASE_ID 를 설정하세요. "
            "Notion 에서 빈 데이터베이스를 하나 만들고 그 ID를 넣으면 "
            "속성은 ensure_content_schema() 가 채웁니다."
        )
    data = _request("GET", f"/databases/{NOTION_CONTENT_DATABASE_ID}")
    sources = data.get("data_sources", [])
    if not sources:
        raise RuntimeError(
            f"Content DB {NOTION_CONTENT_DATABASE_ID} 에 data source가 없습니다. "
            "통합(Integration)이 이 DB에 연결(공유)돼 있는지 확인하세요."
        )
    return sources[0]["id"]


def ensure_content_schema(data_source_id: str, news_data_source_id: str = "") -> None:
    """없는 속성을 자동으로 추가한다. (notion_store.ensure_schema 와 같은 방식)

    Relation 은 따로 시도한다. 데이터 소스 기반 스키마는 버전에 민감해
    실패할 수 있는데, 실패해도 파이프라인은 원문ID 속성으로 돌아간다.
    사람이 클릭해 오갈 편의만 없어질 뿐이라 경고만 남기고 진행한다.
    """
    current = _request("GET", f"/data_sources/{data_source_id}")
    existing = current.get("properties", {})

    # 제목 속성은 만들 수 없고 이름만 바꿀 수 있다.
    # Notion 이 기본으로 붙인 '이름' 을 '콘텐츠제목' 으로 고쳐 둔다.
    # 실패해도 상관없다. create_content() 는 resolve_title_prop() 으로
    # 실제 이름을 찾아 쓰기 때문이다.
    title_prop = _find_title_prop(existing)
    if title_prop != PROP_TITLE:
        try:
            _request(
                "PATCH",
                f"/data_sources/{data_source_id}",
                json={"properties": {title_prop: {"name": PROP_TITLE}}},
            )
            log.info(f"제목 속성 이름 변경: '{title_prop}' -> '{PROP_TITLE}'")
            _title_prop_cache[data_source_id] = PROP_TITLE
        except Exception as e:
            log.warning(f"제목 속성 이름 변경 실패(그대로 '{title_prop}' 사용): {e}")
            _title_prop_cache[data_source_id] = title_prop
    else:
        _title_prop_cache[data_source_id] = PROP_TITLE

    patch: dict[str, dict] = {}
    for name, definition in EXPECTED_PROPS.items():
        if name not in existing:
            patch[name] = definition

    # 상태·유형 Select 옵션 보강·개명
    #   보강 — 없는 옵션을 덧붙인다
    #   개명 — '승인대기' -> '초안대기'. 옵션 id 를 유지하므로 기존 행의
    #          값도 함께 따라온다 (notion_store._merge_select_options 참고)
    notes: list[str] = [f"{n}(신규)" for n in patch]
    for prop in (PROP_STATUS, PROP_KIND):
        if prop not in existing or prop in patch:
            continue
        if existing[prop].get("type") != "select":
            log.warning(f"'{prop}' 속성이 select 가 아닙니다 ({existing[prop].get('type')})")
            continue
        options, changes = _merge_select_options(
            prop, existing[prop], EXPECTED_PROPS[prop]["select"]["options"],
            renames=RENAMED_OPTIONS.get(prop, {}),
        )
        if options:
            patch[prop] = {"select": {"options": options}}
            notes.append(f"{prop}({', '.join(changes)})")

    if patch:
        log.info(f"Content 스키마 보정: {' · '.join(notes)}")
        _request("PATCH", f"/data_sources/{data_source_id}", json={"properties": patch})
    else:
        log.info("Content 스키마 확인 완료 (변경 없음)")

    if news_data_source_id and PROP_NEWS not in existing:
        try:
            _request(
                "PATCH",
                f"/data_sources/{data_source_id}",
                json={
                    "properties": {
                        PROP_NEWS: {
                            "relation": {
                                "data_source_id": news_data_source_id,
                                "type": "dual_property",
                                "dual_property": {},
                            }
                        }
                    }
                },
            )
            log.info(f"Relation 속성 '{PROP_NEWS}' 생성 완료")
        except Exception as e:
            log.warning(
                f"Relation 속성 자동 생성 실패: {e}\n"
                f"  Notion UI 에서 '{PROP_NEWS}' 관계형 속성을 News DB로 직접 연결하세요. "
                f"없어도 파이프라인은 '{PROP_NEWS_IDS}' 로 동작합니다."
            )


# ── 텍스트 헬퍼 ────────────────────────────────────────
def _rt(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:MAX_TEXT_LEN]}}]}


def _plain(prop: dict) -> str:
    parts = prop.get(prop.get("type", ""), [])
    if isinstance(parts, list):
        return "".join(p.get("plain_text", "") for p in parts)
    return ""


def _flat_subtopics(brief: DraftBrief) -> list[str]:
    """묶음 전체의 소주제를 순서대로 편다. 기사 1건이 챕터 1개인 묶음도 있고,
    긴 기사 1건이 챕터 4개인 경우도 있어 기사 경계 없이 이어 붙인다."""
    out: list[str] = []
    for a in brief.articles:
        out.extend(a.subtopics)
    return out[:MAX_SUBTOPICS]


# ── 페이지 본문 ────────────────────────────────────────
def _review_blocks(brief: DraftBrief, reason: str) -> list[dict]:
    """승인 화면. Content 페이지 하나만 보고 판단할 수 있게 만든다.

    소주제를 to_do 로 두는 이유: 4개 중 3개만 쓰고 싶을 때 체크를 풀어
    표시할 수 있다. 현재 파이프라인은 이 체크를 읽지 않지만, 사람이
    메모를 남기는 것보다 위치가 분명하다.
    """
    lo, hi, chapters = brief.target_length()
    blocks: list[dict] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"text": {"content": reason[:MAX_TEXT_LEN]}}],
                "icon": {"type": "emoji", "emoji": "💡"},
            },
        },
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "text": {
                        "content": (
                            f"기사 {len(brief)}건 · 원문 {brief.source_chars:,}자 "
                            f"→ 목표 {lo:,}~{hi:,}자 · 챕터 {chapters}개"
                        )
                    }
                }]
            },
        },
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": SUBTOPIC_HEADING}}]},
        },
    ]

    for i, topic in enumerate(_flat_subtopics(brief), 1):
        blocks.append({
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"text": {"content": f"{i}. {topic}"[:MAX_TEXT_LEN]}}],
                "checked": True,
            },
        })

    blocks.append({"object": "block", "type": "divider", "divider": {}})
    blocks.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"text": {"content": f"{SOURCE_HEADING} — {len(brief)}건"}}]
        },
    })
    return blocks


def _source_toggle(idx: int, art: SourceArticle) -> dict:
    """기사 1건을 접힌 토글로. 제목 줄에 소주제를 함께 적어 어느 챕터가
    이 기사에서 나왔는지 펼치지 않고도 알 수 있게 한다."""
    label = f"{idx}. {art.title} ({art.source_chars:,}자)"
    if art.subtopics:
        label += f" → {' / '.join(art.subtopics)}"

    children: list[dict] = []
    if art.url:
        children.append({"object": "block", "type": "bookmark", "bookmark": {"url": art.url}})
    children.extend(text_to_blocks(art.content))

    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"text": {"content": label[:MAX_TEXT_LEN]}}],
            "children": children[:MAX_BLOCKS_PER_REQUEST],
        },
    }


def provisional_title(brief: DraftBrief, subtopics: list[str]) -> str:
    """승인 단계에서 쓸 임시 이름.

    이 시점에는 아직 블로그 제목이 없다. 제목은 write_all() 안에서 LLM 이
    초안 첫 줄로 만들기 때문이다. brief.title_hint 를 그대로 쓰면 4건을
    묶은 글에 첫 기사 제목만 붙어 나머지 3건이 가려진다.
    (실측: 4건 묶음의 이름이 '아시안게임, 버추얼 태권도 캠프 - 전라매일')

    그래서 소주제로 무엇을 다룰 글인지 알리고, ⏳ 로 아직 확정 전임을
    표시한다. publish 가 초안을 쓴 뒤 update_content_title() 로 바꾼다.
    """
    head = (subtopics[0] if subtopics else brief.title_hint) or "(내용 없음)"
    if len(brief) > 1:
        return f"⏳ {head} 외 {len(brief) - 1}건"
    return f"⏳ {head}"


def update_content_title(data_source_id: str, page_id: str, title: str) -> None:
    """초안이 정한 진짜 제목으로 바꾼다. (publish 단계에서 호출)"""
    prop = resolve_title_prop(data_source_id)
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": {prop: {"title": [{"text": {"content": title[:MAX_TEXT_LEN]}}]}}},
    )


def extract_draft_title(markdown: str) -> str:
    """초안에서 제목을 뽑는다.

    draft_prompt 의 출력 형식: '첫 줄은 제목입니다. # 없이 평문으로 씁니다.'
    LLM 이 지시를 어기고 #, 따옴표, 블록 마커를 붙이는 경우가 있어 걷어낸다.
    """
    for line in (markdown or "").splitlines():
        line = line.strip()
        if not line:
            continue
        line = line.lstrip("#").strip()
        # '/* 본문 */' 같은 블록 마커가 붙어 있으면 떼어낸다
        if "/*" in line:
            line = line.split("/*")[0].strip()
        return line.strip("\"'“”‘’ ")
    return ""


# ── 생성 ───────────────────────────────────────────────
def create_content(
    data_source_id: str,
    brief: DraftBrief,
    *,
    kind: str,
    reason: str,
    news_page_ids: list[str],
    title: str = "",
    status: str = STATUS_PENDING,
    dry_run: bool = False,
) -> str | None:
    """묶음 하나를 Content 페이지로 만든다. page_id 를 반환한다.

    title 을 주면 그것을 제목으로 쓴다. publish 가 초안을 완성한 뒤에
    부르므로 진짜 블로그 제목이 들어온다. 주지 않으면 '⏳ …' 임시 제목을
    단다 (확정 시점에 만들던 옛 경로).
    """
    lo, hi, chapters = brief.target_length()
    subtopics = _flat_subtopics(brief)
    head_url = next((a.url for a in brief.articles if a.url), "")

    label = title or provisional_title(brief, subtopics)

    title_prop = resolve_title_prop(data_source_id)
    props: dict = {
        title_prop: {"title": [{"text": {"content": label[:MAX_TEXT_LEN]}}]},
        PROP_STATUS: {"select": {"name": status}},
        PROP_KIND: {"select": {"name": KIND_LABEL.get(kind, KIND_TOPIC)}},
        PROP_REASON: _rt(reason),
        PROP_NEWS_IDS: _rt(",".join(news_page_ids)),
        PROP_COUNT: {"number": len(brief)},
        PROP_CHARS: {"number": brief.source_chars},
        PROP_TARGET: _rt(f"{lo:,}~{hi:,}자 · 챕터 {chapters}개"),
    }
    if head_url:
        props[PROP_URL] = {"url": head_url}
    if news_page_ids:
        props[PROP_NEWS] = {"relation": [{"id": pid} for pid in news_page_ids]}
    for name, text in zip(PROP_SUBTOPICS, subtopics):
        props[name] = _rt(text)

    if dry_run:
        log.info(
            f"[dry-run] Content: {label[:40]} "
            f"({len(brief)}건 · {brief.source_chars:,}자 · 소주제 {len(subtopics)}개)"
        )
        return None

    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": props,
        "children": _review_blocks(brief, reason),
    }
    try:
        page = _request("POST", "/pages", json=payload)
    except RuntimeError as e:
        # Relation 속성이 없을 때만 빼고 재시도한다.
        # 예전에는 모든 오류에 재시도해서, 제목 속성이 없다는 진짜 원인이
        # 'Relation 없이 재시도합니다' 로그에 가려졌다.
        if PROP_NEWS not in props or PROP_NEWS not in str(e):
            raise
        log.warning(f"Relation 속성이 없어 빼고 재시도합니다: {e}")
        props.pop(PROP_NEWS)
        payload["properties"] = props
        page = _request("POST", "/pages", json=payload)

    page_id = page["id"]

    # 원문 기사는 토글 하나씩 이어붙인다. 한 번에 보내면 100블록을 넘기 쉽다.
    for i, art in enumerate(brief.articles, 1):
        append_blocks(page_id, [_source_toggle(i, art)])

    return page_id


# ── 조회 ───────────────────────────────────────────────
def _load_source(news_page_id: str) -> SourceArticle | None:
    """News 페이지에서 SourceArticle 을 복원한다.

    본문은 속성이 아니라 페이지 블록에 있으므로 fetch_page_body() 로 읽는다.
    (원문 토글 구조와 구버전 평면 구조를 모두 처리한다)
    """
    try:
        page = _request("GET", f"/pages/{news_page_id}")
    except Exception as e:
        log.warning(f"News 페이지 조회 실패 ({news_page_id[:8]}): {e}")
        return None

    props = page.get("properties", {})

    def plain(name: str) -> str:
        return _plain(props.get(name, {}))

    title = ""
    for prop in props.values():
        if prop.get("type") == "title":
            title = _plain(prop)
            break

    subtopics = [
        plain(f"소주제{i}") for i in range(1, 5) if plain(f"소주제{i}")
    ]
    body = fetch_page_body(news_page_id)
    if not body:
        log.warning(f"원문 본문을 찾지 못함: {title[:40]}")

    return SourceArticle(
        title=title,
        url=(props.get("URL") or {}).get("url") or "",
        date=((props.get("날짜") or {}).get("date") or {}).get("start", ""),
        source=plain("출처"),
        subtopics=subtopics,
        matched_keyword=(plain("매칭키워드").split(",")[0] or "").strip(),
        content=body,
    )


# publish 가 뉴스 DB 에서 직접 묶음을 되살릴 때 쓴다.
# (Content 행이 아직 없는 시점이라 fetch_pending_contents 를 쓸 수 없다)
load_source = _load_source


def _redistribute(articles: list[SourceArticle], edited: list[str]) -> None:
    """Notion 에서 사람이 고친 소주제를 기사별로 되돌려 넣는다.

    개수가 원래와 같을 때만 반영한다. 사람이 줄이거나 늘렸으면 어느
    기사의 챕터가 사라진 것인지 알 수 없어, 잘못 배정하느니 원본을 쓴다.
    """
    original = sum(len(a.subtopics) for a in articles)
    if not edited or len(edited) != original:
        return
    cursor = 0
    for a in articles:
        n = len(a.subtopics)
        a.subtopics = edited[cursor : cursor + n]
        cursor += n


def fetch_pending_contents(data_source_id: str) -> list[dict]:
    """상태='초안대기' 인 Content 목록. publish 가 이걸 집어 간다.

    승인 체크박스로 한 번 더 거르던 것을 없앴다. confirm 을 통과했다는
    것 자체가 승인이므로, 여기서 다시 확인을 요구하면 같은 판단을 두 번
    하게 된다. 실행 시점(어느 것을 오늘 돌릴지)은 publish 를 언제 부르는지로
    조절한다 — Gemini RPD 20 때문에 하루에 다 돌릴 수 없다.

    반환 dict 는 write_all() 이 그대로 받는 모양이다.
      page_id : 초안을 저장할 Content 페이지
      pages   : 함께 '작성완료'로 넘길 News 페이지들
      brief   : DraftBrief (묶음 정보)
    """
    body = {
        "filter": {"property": PROP_STATUS, "select": {"equals": STATUS_PENDING}},
        "page_size": 100,
    }

    items: list[dict] = []
    cursor = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _request("POST", f"/data_sources/{data_source_id}/query", json=body)

        for page in data.get("results", []):
            props = page.get("properties", {})
            title = ""
            for prop in props.values():
                if prop.get("type") == "title":
                    title = _plain(prop)
                    break

            news_ids = [
                pid.strip()
                for pid in _plain(props.get(PROP_NEWS_IDS, {})).split(",")
                if pid.strip()
            ]
            if not news_ids:
                log.warning(f"원문ID가 비어 건너뜀: {title[:40]}")
                continue

            articles = [a for a in (_load_source(pid) for pid in news_ids) if a]
            if not articles:
                log.warning(f"원문 기사를 하나도 읽지 못해 건너뜀: {title[:40]}")
                continue

            edited = [
                _plain(props[name])
                for name in PROP_SUBTOPICS
                if name in props and _plain(props[name])
            ]
            _redistribute(articles, edited)

            brief = DraftBrief(
                articles=articles,
                title_hint=title,
                matched_keyword=next(
                    (a.matched_keyword for a in articles if a.matched_keyword), ""
                ),
            )
            items.append({
                "page_id": page["id"],          # 초안이 들어갈 곳
                "pages": news_ids,              # '작성완료'로 넘길 News 페이지
                "title": title,
                "url": (props.get(PROP_URL) or {}).get("url", ""),
                "subtopics": _flat_subtopics(brief),
                "body": articles[0].content,    # planned 삽화가 참고할 대표 본문
                "brief": brief,
                "edited": page.get("last_edited_time", ""),
            })

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return items


# ── 상태 변경 ──────────────────────────────────────────
def fetch_contents_by_status(data_source_id: str, status: str) -> list[dict]:
    """상태로 Content 목록을 읽는다. [{page_id, title, url, created, edited}]

    fetch_pending_contents() 와 달리 원문 기사를 따라가지 않는다. 초안에
    삽화를 붙이는(images) 것처럼 페이지 본문만 필요한 경우에 쓴다.
    Content 한 건마다 News 페이지를 4번 읽는 비용을 치를 이유가 없다.
    """
    body: dict = {
        "filter": {"property": PROP_STATUS, "select": {"equals": status}},
        "page_size": 100,
    }
    items: list[dict] = []
    cursor = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _request("POST", f"/data_sources/{data_source_id}/query", json=body)
        for page in data.get("results", []):
            props = page.get("properties", {})
            title = ""
            for prop in props.values():
                if prop.get("type") == "title":
                    title = _plain(prop)
                    break
            items.append({
                "page_id": page["id"],
                "title": title,
                "url": (props.get(PROP_URL) or {}).get("url", ""),
                "created": page.get("created_time", ""),
                "edited": page.get("last_edited_time", ""),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return items


# 옛 이름. 다른 모듈이 아직 참조할 수 있어 남겨 둔다.
# (승인 체크박스가 없어졌으므로 이름과 동작이 어긋난다 — 새 코드는
#  fetch_pending_contents 를 쓸 것)
fetch_approved_contents = fetch_pending_contents


def mark_content_written(page_id: str) -> None:
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": {PROP_STATUS: {"select": {"name": STATUS_WRITTEN}}}},
    )


def save_content_quality(page_id: str, report_text: str, model: str = "") -> None:
    """품질 측정 요약을 '형태소' 속성에 남긴다. (notion_store 와 같은 형식)"""
    text = report_text.strip()
    if len(text) > MAX_TEXT_LEN:
        text = text[: MAX_TEXT_LEN - 20].rstrip() + "\n… (이하 생략)"
    props: dict = {PROP_MORPH: _rt(text)}
    if model:
        props[PROP_WRITER_MODEL] = _rt(model)
    _request("PATCH", f"/pages/{page_id}", json={"properties": props})
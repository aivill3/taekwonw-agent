"""Notion DB 저장. (구 notion_writer.py 이식)

핵심 (2025-09-03 이후 API 변경 반영):
  - 최신 Notion-Version: 2026-03-11
  - 데이터베이스는 여러 "data source"를 담는 컨테이너.
    페이지 생성 시 부모는 database_id가 아니라 data_source_id.
    → GET /v1/databases/{id} 로 data_sources[0].id 를 먼저 조회해서 사용.
  - 리치텍스트 1개 content 최대 2000자 → 긴 문단은 분할
  - children 1회 요청당 최대 100블록 → 초과분은 PATCH로 이어붙임
  - 429(rate limit)는 Retry-After만큼 대기 후 재시도

속성은 save_all() 시작 시 ensure_schema()가 자동으로 확인·생성한다.
   (수동 생성 시 이름 오타로 validation_error가 나는 것을 방지)
   PROP_* 상수 이름을 바꾸면 그 이름으로 새 속성이 만들어진다.
   기대 스키마:
     제목(Title) · URL(URL) · 날짜(Date) · 출처(Text) · 상태(Select)
     승인(Checkbox) · 키워드점수(Number) · 매칭키워드(Text) · 소주제1~4(Text)
   상태 Select 옵션: 선정됨 / 작성완료 / 보류
"""
import json
import time
import re
from urllib.parse import urlparse

import requests

from config.settings import NOTION_API_KEY, NOTION_DATABASE_ID
from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)

NOTION_VERSION = "2026-03-11"
API_BASE = "https://api.notion.com/v1"

# --- DB 속성 이름 매핑 ---
PROP_TITLE = "제목"     # type: title
PROP_URL = "URL"        # type: url
PROP_DATE = "날짜"      # type: date
PROP_SOURCE = "출처"    # type: rich_text (URL 도메인)
PROP_STATUS = "상태"    # type: select
PROP_APPROVED = "승인"  # type: checkbox — 사람이 체크 (승인 게이트)
PROP_SCORE = "키워드점수"  # type: number — 당일 1위=100 환산 점수 (읽기 쉬운 지표)
PROP_KEYWORDS = "매칭키워드"  # type: rich_text — 점수에 기여한 키워드 (선정 근거)
PROP_DRAFT = "블로그원고"    # type: rich_text — 생성된 블로그 초안 (2000자 상한)
PROP_WRITER_MODEL = "작성모델"  # type: rich_text — 초안을 쓴 모델 (검토 강도 판단용)
PROP_MORPH = "형태소"    # type: rich_text — 품질 측정 요약 (감정·형태소·금칙어·구조)
PROP_MODE = "발행구분"   # type: select — 단독 | 묶음 | 보류 (본문 길이 기준)
PROP_BUNDLE = "묶음"     # type: select — 어느 글에 들어갈지. 보드 뷰의 그룹 기준

# ── 묶음 슬롯 ─────────────────────────────────────────
# 보드 뷰에서 카드를 끌어 옮길 칸이다. content 가 자동 배정하고,
# 사람이 드래그로 고친 뒤 confirm 이 확정한다.
#
# 날짜를 라벨에 넣지 않는 이유: '0831-묶음1' 처럼 만들면 select 옵션이
# 매일 늘어나 몇 달 뒤엔 수백 개가 된다. 슬롯을 재사용하고 보드 뷰에
# '상태 = 선정됨' 필터를 걸면 오늘 것만 보인다.
# 확정된 묶음의 고유 이름. '단독1' 같은 슬롯은 열 칸뿐이라 회차마다
# 돌려쓴다. 확정 시점에 슬롯을 비우고 이 값을 대신 붙여, 다음 회차가
# 같은 칸을 써도 앞 회차와 섞이지 않게 한다.
#
#   확정 전   묶음=단독1        묶음ID=(빈값)        상태=선정됨
#   확정 후   묶음=단독1        묶음ID=20260902-01   상태=초안요청
#
# select 가 아니라 rich_text 인 이유: 매일 새 값이 생기는데 select 옵션은
# API 로 지울 수 없어 한 달이면 수백 개가 쌓인다.
PROP_BUNDLE_ID = "묶음ID"

# 발행하지 않는 카드가 가는 칸은 두 개다. 하나로 두면 성격이 다른 둘이
# 섞여, confirm 이 어느 쪽인지 구분하지 못한다.
#
#   대기 — 코드가 넣는다. 4건을 못 채운 묶음이다. 다음 수집분과 다시
#          묶여야 하므로 confirm 이 상태를 건드리지 않는다 ('선정됨' 유지).
#   제외 — 사람이 끌어다 넣는다. 발행하지 않기로 한 기사다.
#          confirm 이 상태를 '보류'로 넘겨 후보에서 뺀다.
#
# 예전에는 '보류' 한 칸뿐이었다. 그래서 짝이 부족했을 뿐인 기사까지
# confirm 이 '보류' 상태로 넘겨 후보에서 사라지게 만들었다.
BUNDLE_WAIT = "대기"
BUNDLE_EXCLUDE = "제외"

# 예전 단일 칸. 남아 있는 카드를 confirm 이 알아보고 경고하기 위해서만 쓴다.
# 어느 쪽 의도인지 코드가 알 수 없으므로 상태는 바꾸지 않는다.
BUNDLE_LEGACY_HOLD = "보류"

# 카드에는 판정 칩을 붙이지 않는다(2026-09-14).
# '대기'·'제외'·'미배정' 칩은 카드가 놓인 칸과 같은 말을 두 번 하는 것이고,
# 어긋나면 어느 쪽이 맞는지 알 수 없었다. 배치는 칸이, 판정은 보드 밑
# '승인 불가 현황' 콜아웃이 말한다. 칸 제목 옆의 건수는 Notion 이 실시간으로
# 보여주므로 그것과 함께 본다.
BUNDLE_SOLO_SLOTS = [f"단독{i}" for i in range(1, 7)]   # 긴 기사 1건 = 한 편
BUNDLE_GROUP_SLOTS = [f"묶음{i}" for i in range(1, 5)]  # 짧은 기사 4건 = 한 편
BUNDLE_SLOTS = BUNDLE_SOLO_SLOTS + BUNDLE_GROUP_SLOTS

# 발행구분 값. 판정은 agents/drafting/article_grouper.classify_publish_mode() 가 한다.
# 여기 문자열은 노션 Select 옵션을 만들기 위한 것이라 그 모듈과 반드시 일치해야 한다.
MODE_SOLO = "단독"
MODE_BUNDLE = "묶음"
MODE_HOLD = "보류"
PROP_SUBTOPICS = [f"소주제{i}" for i in range(1, 5)]  # type: rich_text ×4

# 상태 흐름:
#   수집됨 ──(자동 선정)──→ 선정됨 ──(버튼)──→ 초안요청 ──(confirm+publish)──→ 작성완료
#      └──(사람이 직접 지정)──→ 선정요청 ──(소주제 생성)──→ 선정됨
#   검토 후 거절하면 보류
#
# 승인은 '보드에 카드를 배치하고 confirm 을 돌리는 것' 자체다. 별도의
# 승인 체크박스는 없앴다. 배치가 곧 의사표시인데 체크를 한 번 더 받으면
# 같은 판단을 두 번 하게 된다.
STATUS_COLLECTED = "수집됨"    # 수집·정제만 된 상태 (사람이 훑어보는 목록)
STATUS_REQUESTED = "선정요청"  # 사람이 직접 고름 → 소주제 생성 대기
STATUS_DEFAULT = "선정됨"      # 소주제까지 생성. 보드에서 묶음 배치 대기
STATUS_REQUESTED_DRAFT = "초안요청"  # 사람이 [초안 작성] 을 눌렀다
STATUS_WRITTEN = "작성완료"    # 블로그 초안 생성 완료
STATUS_HOLD = "보류"

# 확정과 작성 사이에 상태를 두지 않는다
# ---------------------------------
# 예전에는 '초안대기' 가 하나 더 있었다. confirm 이 '선정됨' 을 '초안대기'
# 로 바꾸고, 사람이 그중에서 오늘 돌릴 것을 골랐다.
#
# 그런데 사람이 하는 판단은 하나뿐이다 — "이 배치로 글을 쓴다". 배치를
# 끝내고 버튼을 누르면 그것으로 끝나야 한다. 중간 상태는 코드가 스쳐
# 지나갈 뿐인데 보드에 칸이 하나 더 생겨 혼란만 만들었다.
#
#   선정됨 ──[초안 작성]──→ 초안요청 ──(폴링)──→ 작성완료
#                                       confirm + publish
#
# Notion 버튼은 속성 값만 바꿀 수 있고(웹훅 보내기는 유료 플랜), GitHub 를
# 직접 부르지 못한다. 그래서 버튼이 상태를 '초안요청' 으로 바꾸고, 폴링이
# 그 값을 보고 확정과 작성을 이어서 실행한다.
#
# 실패하면 '선정됨' 으로 되돌린다. '초안요청' 으로 남으면 폴링이 같은
# 묶음을 30분마다 재시도해 Gemini 할당량을 갉아먹는다. 되돌리면 카드가
# 보드 제자리로 돌아와 사람이 고친 뒤 다시 누를 수 있다.

# 옵션 이름이 바뀐 이력. ensure_schema() 가 옛 이름을 찾으면 새 이름으로
# 바꾼다. 옵션을 '추가'하면 기존 행의 값은 옛 이름에 남아 코드가 못 찾지만,
# '이름 변경'은 옵션 id 가 그대로라 행 값이 함께 따라온다.
RENAMED_OPTIONS: dict[str, dict[str, str]] = {
    PROP_STATUS: {"콘텐츠연결": STATUS_REQUESTED_DRAFT},
}

# --- API 제약 ---
MAX_BLOCKS_PER_REQUEST = 100
MAX_TEXT_LEN = 2000
DRAFT_HEADING = "블로그 초안"  # 원문과 초안을 구분하는 헤딩

# 원문 기사를 감싸는 토글의 제목 접두사.
# 원문을 접어두면 페이지를 열었을 때 초안이 바로 보인다. 초안 검수 때마다
# 기사 20~40문단을 스크롤해 내려가야 했던 것을 없애기 위한 것이다.
# fetch_page_body() 가 이 접두사로 토글을 찾아 내부를 읽는다.
ORIGIN_HEADING = "📄 원문 기사"


# ── HTTP 헬퍼 ──────────────────────────────────────────
def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# 재시도할 가치가 있는 네트워크 오류. 응답 코드가 없어 상태 코드로는 못 잡는다.
# 사내망 프록시나 SSL 검사 장비를 거치면 TLS 핸드셰이크가 간헐적으로 끊긴다.
# 재시도가 없으면 이 한 번의 실패로 파이프라인 전체가 중단된다.
# (실측 2026-08-14: 첫 resolve_data_source_id 에서 WinError 10054 로 즉시 종료)
_RETRYABLE_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

# Notion 쪽 일시 장애. 잠시 뒤 같은 요청이 성공하는 경우가 대부분이다.
_RETRYABLE_STATUS = {500, 502, 503, 504}

RETRY_BACKOFF = 2.0  # 재시도 대기(초): 2 → 4 → 6


def _request(method: str, path: str, *, max_retries: int = 3, **kwargs) -> dict:
    """Notion API 요청. 429·5xx·연결 오류를 재시도한다."""
    url = f"{API_BASE}{path}"
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
        except _RETRYABLE_ERRORS as e:
            if attempt < max_retries:
                wait = RETRY_BACKOFF * (attempt + 1)
                log.warning(
                    f"Notion 연결 오류({type(e).__name__}), {wait:.0f}초 후 재시도 "
                    f"({attempt + 1}/{max_retries}): {method} {path}"
                )
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"Notion API {method} {path} 연결 실패(재시도 {max_retries}회 초과): {e}"
            ) from e

        if resp.status_code == 429 and attempt < max_retries:
            time.sleep(float(resp.headers.get("Retry-After", "1")))
            continue

        if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
            wait = RETRY_BACKOFF * (attempt + 1)
            log.warning(
                f"Notion 서버 오류({resp.status_code}), {wait:.0f}초 후 재시도 "
                f"({attempt + 1}/{max_retries}): {method} {path}"
            )
            time.sleep(wait)
            continue

        if not resp.ok:
            # 어떤 속성이 안 맞는지 등 원인이 담긴 message를 그대로 노출
            raise RuntimeError(f"Notion API {method} {path} 실패 [{resp.status_code}]: {resp.text}")
        return resp.json()
    raise RuntimeError(f"Notion API {method} {path} 재시도 초과")


# ── data source 해석 ───────────────────────────────────
def resolve_data_source_id(database_id: str = NOTION_DATABASE_ID) -> str:
    data = _request("GET", f"/databases/{database_id}")
    sources = data.get("data_sources", [])
    if not sources:
        raise RuntimeError(
            f"데이터베이스 {database_id} 에 data source가 없습니다. "
            "통합(Integration)이 이 DB에 연결(공유)돼 있는지 확인하세요."
        )
    return sources[0]["id"]


# ── 스키마 자동 보정 ───────────────────────────────────
# 코드가 기대하는 속성 정의. DB에 없으면 자동으로 추가한다
# (수동 생성 시 이름 오타로 validation_error가 나는 것을 방지)
EXPECTED_PROPS: dict[str, dict] = {
    PROP_URL: {"url": {}},
    PROP_DATE: {"date": {}},
    PROP_SOURCE: {"rich_text": {}},
    PROP_APPROVED: {"checkbox": {}},
    PROP_SCORE: {"number": {"format": "number"}},
    PROP_KEYWORDS: {"rich_text": {}},
    # PROP_DRAFT 는 자동 생성하지 않는다. 초안은 페이지 본문에 저장하므로
    # (publish_workflow 참고) 이 속성은 항상 비어 있고, 새 DB를 만들 때마다
    # 쓰이지 않는 열이 하나씩 생긴다. 기존 DB에 이미 있는 속성은 지우지 않는다.
    PROP_BUNDLE: {
        "select": {
            "options": (
                [{"name": n, "color": "purple"} for n in BUNDLE_SOLO_SLOTS]
                + [{"name": n, "color": "blue"} for n in BUNDLE_GROUP_SLOTS]
                + [
                    {"name": BUNDLE_WAIT, "color": "yellow"},
                    {"name": BUNDLE_EXCLUDE, "color": "gray"},
                ]
            )
        }
    },
    PROP_BUNDLE_ID: {"rich_text": {}},
    PROP_WRITER_MODEL: {"rich_text": {}},
    PROP_MORPH: {"rich_text": {}},
    PROP_MODE: {
        "select": {
            "options": [
                {"name": MODE_SOLO, "color": "green"},
                {"name": MODE_BUNDLE, "color": "blue"},
                {"name": MODE_HOLD, "color": "gray"},
            ]
        }
    },
    PROP_STATUS: {
        "select": {
            "options": [
                {"name": STATUS_COLLECTED, "color": "default"},
                {"name": STATUS_REQUESTED, "color": "orange"},
                {"name": STATUS_DEFAULT, "color": "yellow"},
                {"name": STATUS_REQUESTED_DRAFT, "color": "pink"},
                {"name": STATUS_WRITTEN, "color": "green"},
                {"name": STATUS_HOLD, "color": "gray"},
            ]
        }
    },
    **{name: {"rich_text": {}} for name in PROP_SUBTOPICS},
}


def _merge_select_options(
    name: str,
    existing_prop: dict,
    wanted: list[dict],
    renames: dict[str, str] | None = None,
) -> tuple[list[dict] | None, list[str]]:
    """Select 옵션을 보강·개명한다. (보낼 옵션 목록 | None, 로그용 변경 내역)

    두 가지를 한 번에 처리한다.

    개명 — RENAMED_OPTIONS 에 적힌 옛 이름을 찾으면 id 를 유지한 채 이름만
           바꾼다. 옵션 id 가 그대로이므로 그 값을 쓰던 기존 행들도 새 이름을
           따라온다. 새 옵션을 '추가'하는 방식이었다면 기존 행은 옛 이름에
           남아 코드 조회에 걸리지 않는다.
    보강 — 아직 없는 옵션을 뒤에 덧붙인다. 사람이 Notion 에서 추가한
           옵션은 건드리지 않는다.

    renames 를 주지 않으면 이 모듈의 RENAMED_OPTIONS 를 쓴다. Content DB 는
    속성 이름은 같아도(둘 다 '상태') 개명 이력이 달라 따로 넘긴다.
    """
    options = [dict(o) for o in existing_prop.get("select", {}).get("options", [])]
    changes: list[str] = []

    if renames is None:
        renames = RENAMED_OPTIONS.get(name, {})
    current_names = {o["name"] for o in options}
    for old_name, new_name in renames.items():
        if new_name in current_names:
            continue  # 이미 바뀌었거나 사람이 직접 만들어 둠
        for o in options:
            if o["name"] == old_name:
                o["name"] = new_name
                current_names.add(new_name)
                changes.append(f"{old_name} -> {new_name}")
                break

    missing = [o for o in wanted if o["name"] not in current_names]
    if missing:
        options += missing
        changes += [f"+{o['name']}" for o in missing]

    return (options if changes else None), changes


def ensure_schema(data_source_id: str) -> None:
    """DB에 없는 속성을 자동 추가하고, Select 옵션을 보강·개명한다.

    2025-09-03 이후 API에서는 속성이 데이터베이스가 아니라 data source에 붙는다.
    → PATCH /data_sources/{id} 로 수정한다.

    옵션 보강을 '상태'에만 하던 것을 모든 Select 속성으로 넓혔다. '묶음'처럼
    이미 만들어진 속성에 나중에 옵션을 추가할 때 (보류 -> 대기·제외) 손으로
    만들어야 했기 때문이다."""
    current = _request("GET", f"/data_sources/{data_source_id}")
    existing = current.get("properties", {})

    patch: dict[str, dict] = {}
    notes: list[str] = []

    # 1) 누락 속성 추가
    for name, definition in EXPECTED_PROPS.items():
        if name not in existing:
            patch[name] = definition
            notes.append(f"{name}(신규)")

    # 2) 기존 Select 속성의 옵션 보강·개명
    for name, definition in EXPECTED_PROPS.items():
        if name in patch or name not in existing:
            continue
        if "select" not in definition:
            continue
        if existing[name].get("type") != "select":
            # 사람이 타입을 바꿔 둔 경우. 옵션을 밀어 넣으면 400 이 난다.
            log.warning(f"'{name}' 속성이 select 가 아닙니다 ({existing[name].get('type')})")
            continue
        options, changes = _merge_select_options(
            name, existing[name], definition["select"]["options"]
        )
        if options:
            patch[name] = {"select": {"options": options}}
            notes.append(f"{name}({', '.join(changes)})")

    if not patch:
        log.info("Notion 스키마 확인 완료 (변경 없음)")
        return

    log.info(f"Notion 스키마 보정: {' · '.join(notes)}")
    _request("PATCH", f"/data_sources/{data_source_id}", json={"properties": patch})


# ── 본문 → 블록 변환 ───────────────────────────────────
def _chunk_text(text: str, size: int = MAX_TEXT_LEN):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def text_to_blocks(content: str) -> list[dict]:
    """개행 기준 문단 분리, 2000자 초과 문단은 여러 블록으로 분할."""
    blocks: list[dict] = []
    for para in content.split("\n"):
        para = para.strip()
        if not para:
            continue
        for chunk in _chunk_text(para):
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })
    return blocks


def origin_toggle(blocks: list[dict], char_count: int) -> dict:
    """원문 블록을 접힌 토글 하나로 감싼다.

    제목에 글자 수를 넣는 이유: 접혀 있어도 이 기사가 단독으로 갈 만큼
    긴지 목록에서 바로 판단할 수 있어야 하기 때문이다.

    children 은 한 요청에 100개까지다. 초과분은 create_page() 가
    토글 id를 찾아 이어붙인다.
    """
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{
                "type": "text",
                "text": {"content": f"{ORIGIN_HEADING} ({char_count:,}자)"},
            }],
            "children": blocks[:MAX_BLOCKS_PER_REQUEST],
        },
    }


def find_origin_toggle_id(page_id: str) -> str | None:
    """페이지에서 원문 토글의 block id를 찾는다. 없으면 None (구버전 평면 구조)."""
    for b in list_blocks(page_id):
        if b.get("type") != "toggle":
            continue
        if _block_text(b).startswith(ORIGIN_HEADING):
            return b["id"]
    return None

NOTICE_MARKER = "승인 불가 현황"

# 아직 판정이 돌지 않은 날 콜아웃에 남기는 자리표시.
# 아침에 reset_notice 가 써 넣고, 첫 confirm 이 현황으로 덮어쓴다.
NOTICE_EMPTY = "(오늘 아직 판정 전)"

# 콜아웃 첫 줄에서 날짜를 뽑는다. '승인 불가 현황 — 2026-09-12' 형식.
RE_NOTICE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def find_notice_block_id(page_id: str) -> str | None:
    """페이지에서 '승인 불가 현황' 콜아웃의 block id를 찾는다.

    표식 문구(NOTICE_MARKER)로 시작하는 콜아웃을 찾는다. 없으면 None —
    이 경우 사람이 그 페이지에 콜아웃을 한 번 만들고 첫 줄에 이 문구를
    적어 둬야 한다.
    """
    for b in list_blocks(page_id):
        if b.get("type") != "callout":
            continue
        if _block_text(b).startswith(NOTICE_MARKER):
            return b["id"]
    return None


def write_notice(page_id: str, body: str) -> bool:
    """'승인 불가 현황' 콜아웃을 통째로 다시 쓴다. 성공하면 True.

    이어붙이지 않고 덮어쓰는 이유
    ---------------------------
    예전에는 회차별 거부 내역을 아래로 쌓았다(append_notice). 카드에 판정
    칩이 있어서 콜아웃은 '사유 보관함' 역할만 하면 됐기 때문이다.

    칩을 걷어낸 뒤로 이 콜아웃이 판정을 읽을 유일한 창구가 됐다. 그러면
    담아야 할 것이 '오늘 있었던 일'이 아니라 '지금 무엇을 손봐야 하는가'로
    바뀐다. 누적하면 지난 회차에 이미 고친 항목이 섞여 들어가, 무엇이
    현재 상태인지 읽을 수 없다. 2000자 제한에 닿아 뒷부분이 잘리는 문제도
    누적 방식에서만 생긴다.

    지난 회차 이력은 로그와 Slack 에 남는다. 보드는 현재 상태를 본다.

    첫 줄은 여기서 붙인다 — 표식 문구와 갱신 시각. find_notice_block_id()
    가 이 표식으로 블록을 찾으므로 호출자가 임의로 바꾸면 안 된다.
    시각을 넣는 것은, 오늘 아직 판정이 돌지 않았을 때 어제 내용이 지금
    상태인 것처럼 읽히는 것을 막기 위해서다.

    콜아웃을 못 찾으면 경고만 남기고 끝낸다.
    """
    from datetime import datetime

    from config.settings import KST

    block_id = find_notice_block_id(page_id)
    if not block_id:
        log.warning(
            f"'{NOTICE_MARKER}' 콜아웃을 찾지 못했습니다. "
            f"페이지에 콜아웃을 만들고 첫 줄에 이 문구를 적어 주세요."
        )
        return False

    stamp = f"{datetime.now(KST):%Y-%m-%d %H:%M}"
    text = f"{NOTICE_MARKER} — {stamp} 기준\n\n{body}"
    _request(
        "PATCH",
        f"/blocks/{block_id}",
        json={"callout": {"rich_text": [{"text": {"content": text[:MAX_TEXT_LEN]}}]}},
    )
    return True


def reset_notice(page_id: str, *, force: bool = False) -> bool:
    """'승인 불가 현황' 콜아웃이 어제 것이면 비운다. 실제로 비웠으면 True.

    collect 가 매일 아침 도는 시점에 호출해, 전날 판정이 오늘 상태인 것처럼
    읽히는 것을 막는다.

    날짜를 보는 이유
    ---------------
    예전에는 호출되면 무조건 비웠다. collect 를 하루 한 번만 돌린다는
    전제였는데, 오후에 한 번 더 돌리면 그날 오전 판정이 통째로 날아간다.
    헤더 날짜가 오늘이면 손대지 않는다.

    ISO 날짜라 문자열 비교로 충분하다(사전순 == 시간순). 미래 날짜가
    들어와도(시계 틀어짐) 남기는 쪽으로 판정한다.

    날짜를 못 찾으면 비운다. 내용이 없거나 사람이 새로 만든 빈 콜아웃일
    가능성이 높고, 어느 쪽이든 다음 confirm 이 현황으로 덮어쓴다.

    force: 날짜와 무관하게 비운다 (수동 정리·테스트용).
    """
    from datetime import datetime

    from config.settings import KST

    today = f"{datetime.now(KST):%Y-%m-%d}"

    if not force:
        block_id = find_notice_block_id(page_id)
        if not block_id:
            log.warning(f"'{NOTICE_MARKER}' 콜아웃을 찾지 못해 초기화를 건너뜁니다")
            return False
        block = _request("GET", f"/blocks/{block_id}")
        first_line = _block_text(block).split("\n", 1)[0]
        found = RE_NOTICE_DATE.search(first_line)
        if found and found.group(1) >= today:
            log.info(f"'{NOTICE_MARKER}' 는 오늘({today}) 기록입니다 — 그대로 둡니다")
            return False

    if not write_notice(page_id, NOTICE_EMPTY):
        return False
    log.info(f"'{NOTICE_MARKER}' 초기화 완료 ({today})")
    return True


# ── 속성 구성 ──────────────────────────────────────────
def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc or ""
    except Exception:
        return ""


def _build_properties(a: Article, status: str = STATUS_DEFAULT) -> dict:
    title = a.title.strip() or "(제목 없음)"
    props: dict = {
        PROP_TITLE: {"title": [{"text": {"content": title[:MAX_TEXT_LEN]}}]},
        PROP_STATUS: {"select": {"name": status}},
    }
    if a.url:
        props[PROP_URL] = {"url": a.url}
        props[PROP_SOURCE] = {"rich_text": [{"text": {"content": _domain_of(a.url)}}]}
    if a.published:
        props[PROP_DATE] = {"date": {"start": a.published}}

    # 승인 게이트: 체크박스는 기본 false로 생성 → 사람이 검토 후 체크
    props[PROP_APPROVED] = {"checkbox": False}
    # 원시 점수는 날마다 범위가 달라 읽기 어려우므로 당일 1위=100 환산값을 저장
    props[PROP_SCORE] = {"number": a.score_norm}
    if a.matched_keywords:
        # 상위 기여 키워드만 (왜 이 기사가 뽑혔는지 한눈에 보이도록)
        kw_text = ", ".join(a.matched_keywords[:8])
        props[PROP_KEYWORDS] = {"rich_text": [{"text": {"content": kw_text[:MAX_TEXT_LEN]}}]}

    # 소주제 4개를 각각 별도 속성으로 (개별 확인·편집이 쉬움)
    for name, text in zip(PROP_SUBTOPICS, a.subtopics):
        props[name] = {"rich_text": [{"text": {"content": text[:MAX_TEXT_LEN]}}]}

    return props


# ── 중복 확인 (Notion 측, URL 기준) ─────────────────────
def url_exists(data_source_id: str, url: str) -> bool:
    """state.json 유실 대비 최후의 방어선 (예: GitHub Actions 러너 초기화)."""
    body = {"filter": {"property": PROP_URL, "url": {"equals": url}}, "page_size": 1}
    data = _request("POST", f"/data_sources/{data_source_id}/query", json=body)
    return len(data.get("results", [])) > 0


# ── 페이지 생성 ────────────────────────────────────────
def create_page(
    data_source_id: str,
    a: Article,
    *,
    status: str = STATUS_DEFAULT,
    dry_run: bool = False,
) -> str | None:
    blocks = text_to_blocks(a.body_clean)
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": _build_properties(a, status),
        # 원문을 토글 하나로 감싼다. 페이지 최상위에는 이 한 줄만 남고
        # 그 아래에 초안이 붙으므로, 검수할 때 원문을 지나칠 필요가 없다.
        "children": [origin_toggle(blocks, len(a.body_clean))],
    }
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info(f"[dry-run] 총 블록 수: {len(blocks)} (원문 토글 1개로 감쌈)")
        return None

    page = _request("POST", "/pages", json=payload)
    page_id = page["id"]

    # 100블록 초과분은 토글 '안에' 이어붙인다. 페이지에 붙이면 원문 일부가
    # 토글 밖으로 새어나와 초안 위에 그대로 펼쳐진다.
    # 생성 응답에는 자식 블록 id가 없어 한 번 더 조회한다.
    # (40문단 이하가 대부분이라 이 경로는 거의 타지 않는다)
    remaining = blocks[MAX_BLOCKS_PER_REQUEST:]
    if remaining:
        toggle_id = find_origin_toggle_id(page_id)
        if toggle_id:
            append_blocks(toggle_id, remaining)
        else:
            # 토글을 못 찾으면 원문을 잃는 것보다 페이지에 붙이는 편이 낫다
            log.warning(f"원문 토글을 찾지 못해 페이지 본문에 이어붙임: {page_id[:8]}")
            append_blocks(page_id, remaining)
    return page_id


def save_all(
    articles: list[Article],
    *,
    status: str = STATUS_DEFAULT,
    dry_run: bool = False,
    skip_duplicates: bool = True,
) -> list[str]:
    """기사들을 Notion 페이지로 생성. '저장 성공한 URL' 목록을 반환한다.
    (반환값은 state.processed_urls 갱신에 사용 — 실패 건은 다음 실행에서 재시도)"""
    if dry_run:
        data_source_id = "DRY-RUN-DATA-SOURCE-ID"
    else:
        if not NOTION_API_KEY or not NOTION_DATABASE_ID:
            raise SystemExit("환경변수 NOTION_API_KEY / NOTION_DATABASE_ID 를 설정하세요.")
        data_source_id = resolve_data_source_id()
        # 저장 전에 필요한 속성이 모두 있는지 확인하고 없으면 만든다
        try:
            ensure_schema(data_source_id)
        except Exception as e:
            log.warning(f"스키마 자동 보정 실패(수동 추가가 필요할 수 있음): {e}")

    saved_urls: list[str] = []
    skipped, failed = 0, 0
    for a in articles:
        try:
            if skip_duplicates and not dry_run and a.url and url_exists(data_source_id, a.url):
                log.info(f"이미 존재, 스킵: {a.title[:40]}")
                skipped += 1
                saved_urls.append(a.url)  # 이미 존재 = 처리 완료로 간주해 state에 기록
                continue
            page_id = create_page(data_source_id, a, status=status, dry_run=dry_run)
            if not dry_run:
                log.info(f"생성됨: {a.title[:40]} -> {page_id}")
                saved_urls.append(a.url)
        except Exception as e:
            log.warning(f"저장 실패 ({a.title[:40]}): {e}")
            failed += 1
    log.info(f"생성 {len(saved_urls) - skipped} · 스킵 {skipped} · 실패 {failed}")
    return saved_urls


# ── 승인 항목 조회 / 초안 추가 (블로그 작성 단계용) ─────

def _plain_text(prop: dict) -> str:
    """title / rich_text 속성에서 순수 텍스트 추출."""
    parts = prop.get(prop.get("type", ""), [])
    if isinstance(parts, list):
        return "".join(p.get("plain_text", "") for p in parts)
    return ""


def fetch_page_body(page_id: str) -> str:
    """페이지에서 원문 기사 텍스트를 이어붙여 반환한다.

    두 가지 페이지 구조를 모두 읽는다.

      신규   원문이 '📄 원문 기사' 토글 안에 들어 있다.
             → 토글 자식 블록을 내려가서 읽는다.
      구버전 원문이 페이지 최상위에 평면으로 깔려 있다.
             → 예전처럼 최상위 블록을 그대로 읽는다.

    토글 내부를 읽지 않으면 빈 문자열이 돌아오고, 그러면 LLM이 근거 없이
    초안을 쓰게 된다. 구조를 바꿀 때 가장 위험한 지점이라 두 경로를 모두 둔다.

    '블로그 초안' 헤딩 이후는 읽지 않는다. 그 아래는 생성된 초안이라
    원문으로 되먹이면 같은 문장이 증폭된다.
    """
    texts: list[str] = []

    for block in list_blocks(page_id):
        btype = block.get("type", "")
        text = _block_text(block)

        # 초안 영역 시작 → 여기서 멈춘다
        if btype.startswith("heading") and DRAFT_HEADING in text:
            break

        # 신규 구조: 원문 토글 내부로 내려간다
        if btype == "toggle" and text.startswith(ORIGIN_HEADING):
            for child in list_blocks(block["id"]):
                child_text = _block_text(child)
                if child_text:
                    texts.append(child_text)
            continue

        # 구버전 구조: 최상위 평면 블록
        if text:
            texts.append(text)

    return "\n".join(texts)


def fetch_approved(data_source_id: str) -> list[dict]:
    """승인(체크박스 True) + 상태='선정됨' 인 페이지 목록.

    상태 조건을 함께 두는 이유: 이미 '작성완료'된 페이지를 다시 쓰지 않기 위함.
    반환: [{page_id, title, url, subtopics, body}]
    """
    body = {
        "filter": {
            "and": [
                {"property": PROP_APPROVED, "checkbox": {"equals": True}},
                {"property": PROP_STATUS, "select": {"equals": STATUS_DEFAULT}},
            ]
        },
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
                    title = _plain_text(prop)
                    break
            subtopics = [
                _plain_text(props[name])
                for name in PROP_SUBTOPICS
                if name in props and _plain_text(props[name])
            ]
            items.append({
                "page_id": page["id"],
                "title": title,
                "url": (props.get(PROP_URL) or {}).get("url", ""),
                "subtopics": subtopics,
                # 보류 경과일 계산용. 승인 시각을 따로 기록하지 않으므로
                # 페이지 최종 수정 시각을 쓴다(승인 체크가 곧 마지막 수정이다).
                "edited": page.get("last_edited_time", ""),
                # 발행구분은 선정 단계에서만 채워진다. 값이 없는 페이지는
                # {"select": None} 이 오므로(키는 있고 값이 null) 두 단계 모두 막는다.
                "mode": ((props.get(PROP_MODE) or {}).get("select") or {}).get("name", ""),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return items


def save_quality_report(page_id: str, report_text: str, model: str = "") -> None:
    """품질 측정 요약을 '형태소' 속성에 저장한다.

    초안 본문은 페이지 본문에 넣고(append_illustrated_draft), 이 속성에는
    감정 비율·형태소 통계·금칙어·구조 경고만 담는다. DB 목록에서 어느 초안이
    손봐야 하는지 한눈에 보기 위한 지표다.

    rich_text 속성 1개는 2,000자가 상한이므로 넘치면 잘라 표시한다.
    전문은 data/quality/metrics.jsonl 에 남으므로 여기서는 요약으로 충분하다.
    """
    text = report_text.strip()
    if len(text) > MAX_TEXT_LEN:
        text = text[: MAX_TEXT_LEN - 20].rstrip() + "\n… (이하 생략)"
    props: dict = {PROP_MORPH: {"rich_text": [{"text": {"content": text}}]}}
    if model:
        props[PROP_WRITER_MODEL] = {"rich_text": [{"text": {"content": model[:MAX_TEXT_LEN]}}]}
    _request("PATCH", f"/pages/{page_id}", json={"properties": props})


def markdown_to_blocks(markdown: str) -> list[dict]:
    """간단한 마크다운을 Notion 블록으로 변환 (## 소제목, 나머지는 문단)."""
    blocks: list[dict] = []
    for line in markdown.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            content, btype = line[3:], "heading_2"
        elif line.startswith("# "):
            content, btype = line[2:], "heading_1"
        else:
            content, btype = line, "paragraph"
        for chunk in _chunk_text(content):
            blocks.append({
                "object": "block",
                "type": btype,
                btype: {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })
    return blocks


def mark_written(page_id: str) -> None:
    """상태를 '작성완료'로 변경 (재작성 방지)."""
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": {PROP_STATUS: {"select": {"name": STATUS_WRITTEN}}}},
    )


def finish_article(page_id: str) -> None:
    """초안이 나갔다 — '작성완료'로 넘기고 슬롯을 비운다.

    슬롯을 여기서 비우는 이유
    ----------------------
    '단독1'~'묶음4' 는 열 칸뿐이라 회차마다 돌려쓴다. 초안까지 나간
    기사가 칸을 계속 차지하면 다음 회차 배정분이 같은 칸에 얹힌다.

      실측(2026-09-02): 단독1~3 에 각각 2건씩 포개져 보였다. 한 건은
      이미 작성됐거나 보류된 것이었다.

    확정(confirm) 시점이 아니라 여기서 비우는 것은, 초안을 기다리는
    동안에는 카드가 보드에 남아 있어야 무엇이 진행 중인지 보이기 때문이다.

    둘을 한 번의 PATCH 로 보낸다. 상태만 바뀌고 슬롯이 남으면 다음
    회차와 겹치고, 슬롯만 비고 상태가 남으면 초안을 또 쓴다.
    """
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={
            "properties": {
                PROP_STATUS: {"select": {"name": STATUS_WRITTEN}},
                PROP_BUNDLE: {"select": None},
            }
        },
    )


# ── 파일 업로드 / 이미지 블록 (Direct Upload API) ───────

def upload_file(path) -> str | None:
    """로컬 파일을 Notion 저장소에 업로드하고 file_upload id를 반환한다.

    2단계로 진행한다 (Notion Direct Upload):
      1) POST /file_uploads          — 업로드 슬롯 생성
      2) POST /file_uploads/{id}/send — multipart/form-data 로 실제 전송
    이 방식 덕분에 외부 이미지 호스팅이 필요 없다.
    업로드된 파일은 만료 전에 블록에 붙여야 하므로 바로 사용한다.
    """
    from pathlib import Path as _Path

    path = _Path(path)
    try:
        content_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        created = _request("POST", "/file_uploads", json={
            "filename": path.name,
            "content_type": content_type,
        })
        upload_id = created["id"]

        # send 는 JSON이 아니라 multipart 이므로 헤더를 따로 구성한다
        # (Content-Type 은 requests 가 boundary와 함께 자동 설정하도록 비워둔다)
        headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": NOTION_VERSION,
        }
        with path.open("rb") as f:
            resp = requests.post(
                f"{API_BASE}/file_uploads/{upload_id}/send",
                headers=headers,
                files={"file": (path.name, f, content_type)},
                timeout=60,
            )
        if not resp.ok:
            log.warning(f"파일 업로드 실패 [{resp.status_code}]: {resp.text[:200]}")
            return None
        return upload_id
    except Exception as e:
        log.warning(f"파일 업로드 오류 ({path.name}): {e}")
        return None


def image_block(upload_id: str, caption: str = "") -> dict:
    """업로드된 파일로 이미지 블록을 만든다."""
    block: dict = {
        "object": "block",
        "type": "image",
        "image": {"type": "file_upload", "file_upload": {"id": upload_id}},
    }
    if caption:
        block["image"]["caption"] = [{"text": {"content": caption[:MAX_TEXT_LEN]}}]
    return block


def append_blocks(page_id: str, blocks: list[dict]) -> None:
    """블록을 페이지 본문 끝에 추가한다 (100개 단위로 나눠 전송)."""
    for i in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
        batch = blocks[i : i + MAX_BLOCKS_PER_REQUEST]
        _request("PATCH", f"/blocks/{page_id}/children", json={"children": batch})


def list_blocks(block_id: str) -> list[dict]:
    """페이지(또는 블록)의 자식 블록을 순서대로 모두 가져온다."""
    out: list[dict] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = _request("GET", f"/blocks/{block_id}/children", params=params)
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def _block_text(block: dict) -> str:
    """블록의 평문. 리치텍스트가 없으면 빈 문자열."""
    body = block.get(block.get("type", ""), {})
    return "".join(r.get("plain_text", "") for r in body.get("rich_text", []))


def insert_images_at_slots(page_id: str, images: dict[int, dict]) -> int:
    """이미 저장된 초안의 숫자 자리에 이미지를 끼워 넣는다.

    append_illustrated_draft() 는 초안을 쓰는 시점에 이미지를 함께 넣는다.
    이 함수는 그 반대다. 초안만 먼저 발행해 두고, 시간이 오래 걸리는 이미지
    생성(로컬 diffusers 등)이 끝난 뒤에 나중에 붙이기 위한 것이다.

    숫자만 있는 문단 블록이 이미지 자리다. 앞에서부터 세어 images 의 키와
    맞춘다. 이미지를 넣은 뒤에는 그 번호 문단을 지운다 — 자리를 채웠으면
    안내가 끝난 것이고, 남겨 두면 발행본에 숫자가 그대로 나간다.

    못 채운 자리의 번호는 남긴다. 그 숫자가 '여기가 비었다'는 유일한
    표시다. 발행 시점 경로(append_illustrated_draft)도 같은 규칙이라,
    어느 쪽으로 삽화가 들어갔든 결과가 같다.

    반환: 실제로 삽입된 이미지 수
    """
    if not images:
        return 0

    blocks = list_blocks(page_id)
    inserted = 0
    slot_idx = -1

    # 뒤에서부터 넣는다. 앞에서 넣으면 삽입 때문에 뒤쪽 블록 위치가 밀려
    # 대상이 어긋난다. (after_block 은 블록 id 기준이라 사실 안전하지만,
    # 같은 요청 안에서 순서를 보장하려면 역순이 명확하다)
    targets: list[tuple[str, dict]] = []
    for b in blocks:
        if b.get("type") != "paragraph":
            continue
        if not RE_IMAGE_SLOT.match(_block_text(b).strip()):
            continue
        slot_idx += 1
        img = images.get(slot_idx)
        if img:
            targets.append((b["id"], img))

    for block_id, img in reversed(targets):
        upload_id = upload_file(img["path"])
        if not upload_id:
            continue
        try:
            _request(
                "PATCH",
                f"/blocks/{page_id}/children",
                json={
                    "children": [image_block(upload_id, img.get("caption", ""))],
                    # 삽입 위치. 예전에는 최상위 "after" 였으나 폐기됐고,
                    # 지금 스키마는 after 키가 있으면 400 을 낸다.
                    "position": {
                        "type": "after_block",
                        "after_block": {"id": block_id},
                    },
                },
            )
            inserted += 1
        except Exception as e:
            log.warning(f"이미지 삽입 실패: {e}")
            continue

        # 삽입에 성공한 뒤에만 번호를 지운다. 순서를 바꾸면 삽입이 실패했을 때
        # 자리 표시까지 사라져, 어디가 비었는지 알 수 없게 된다.
        try:
            _request("DELETE", f"/blocks/{block_id}")
        except Exception as e:
            log.warning(f"자리 번호 삭제 실패(숫자가 남습니다): {e}")
    return inserted


RE_IMAGE_SLOT = re.compile(r"^\d{1,2}$")
RE_BLOCK_MARKER = re.compile(r"/\*\s*(소제목|본문|인용구)\s*\*/")


def _insert_slot_number(page_id: str, after_block_id: str, number: int) -> None:
    """지정 블록 바로 뒤에 자리 번호 문단을 넣는다.

    delete_draft_images 가 삽화를 걷어낼 때 그 자리를 표시하려고 쓴다.
    insert_images_at_slots 가 이 문단을 앵커로 찾으므로, 형식은
    RE_IMAGE_SLOT 과 맞아야 한다(숫자만 있는 문단).
    """
    _request(
        "PATCH",
        f"/blocks/{page_id}/children",
        json={
            "children": [{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": str(number)}}]
                },
            }],
            "position": {"type": "after_block", "after_block": {"id": after_block_id}},
        },
    )


def delete_draft_images(page_id: str) -> int:
    """초안 영역의 이미지 블록을 지운다.

    잘못 생성된 삽화를 걷어내고 다시 붙이기 위한 것이다. '블로그 초안' 헤딩
    아래만 대상으로 한다 — 그 위는 원문 기사 영역이라 사람이 붙여 둔 사진이
    있을 수 있고, 그것까지 지우면 복구할 방법이 없다.

    지운 자리에는 번호 문단을 되살린다. 삽화가 들어가면서 번호가 사라졌기
    때문에, 그냥 지우기만 하면 다음 삽입이 앵커를 찾지 못해 0장으로 끝난다
    (발행 시점에 삽화가 들어간 페이지에서는 예전부터 그랬다).

    번호는 문서 순서로 다시 센다. 채운 자리는 이미지 블록으로, 못 채운
    자리는 번호 문단으로 남아 있으므로, 둘을 함께 세면 원래 순번이 나온다.

    한 자리에는 번호 문단이나 이미지 중 하나만 있다고 본다. 삽입할 때
    번호를 지우므로 둘이 함께 있을 수 없다.

    2026-09-14 이전에 만들어진 페이지는 예외다. 그때는 번호를 남기고 그
    뒤에 이미지를 넣어서 한 자리에 두 블록이 있고, 여기를 --redo 로
    돌리면 순번이 두 배로 불어난다(4자리 글에서 1,2,2,4,3,6,4,8).
    그런 페이지는 손으로 번호를 정리한 뒤 돌린다.

    반환: 지운 이미지 수
    """
    started = False
    deleted = 0
    slot_no = 0

    for b in list_blocks(page_id):
        btype = b.get("type", "")
        if btype.startswith("heading"):
            text = "".join(
                r.get("plain_text", "") for r in b.get(btype, {}).get("rich_text", [])
            )
            if DRAFT_HEADING in text:
                started = True
            continue
        if not started:
            continue

        # 아직 못 채운 자리. 번호가 그대로 있으니 순번만 세고 넘어간다.
        if btype == "paragraph" and RE_IMAGE_SLOT.match(_block_text(b).strip()):
            slot_no += 1
            continue
        if btype != "image":
            continue

        slot_no += 1
        try:
            # 번호를 먼저 넣고 이미지를 지운다. 순서를 바꾸면 되살리기가
            # 실패했을 때 자리가 통째로 사라진다.
            _insert_slot_number(page_id, b["id"], slot_no)
            _request("DELETE", f"/blocks/{b['id']}")
            deleted += 1
        except Exception as e:
            log.warning(f"이미지 블록 삭제 실패 ({b['id'][:8]}): {e}")

    if deleted:
        log.info(f"기존 삽화 {deleted}장 삭제")
    return deleted


def append_illustrated_draft(page_id: str, draft: str, images: dict[int, dict]) -> int:
    """원고를 이미지와 함께 페이지 본문 '아래에' 추가한다.

    원문 기사는 그대로 두고 뒤에 덧붙인다(append). 구분선과 헤딩을 앞에 둬서
    fetch_page_body() 가 원문만 읽어갈 수 있게 한다(그 함수는 이 헤딩에서 멈춘다).

    원고는 네이버 에디터에 붙여넣을 평문이므로 헤딩·인용 블록으로 변환하지 않는다.
    `/* 소제목 */` 같은 마커는 발행 담당자가 서식을 적용할 위치 표시이므로
    지우지 않고 그대로 남긴다. 변환하면 복사해 붙여넣을 원문이 사라진다.

    빈 줄로 나뉜 덩어리 하나가 문단 블록 하나가 된다. 줄바꿈은 블록 안에
    보존된다(Notion rich_text 는 개행을 그대로 담는다). 줄마다 블록을 만들면
    150줄짜리 글이 150블록이 되어 요청이 쪼개지고 복사도 어려워진다.

    images: {슬롯 인덱스(0-based): {"path": Path, "caption": str}}
      숫자만 있는 줄이 이미지 자리다. 앞에서부터 순서대로 대응시킨다.
    반환: 실제로 삽입된 이미지 수
    """
    blocks: list[dict] = [
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block",
            "type": "heading_1",
            "heading_1": {"rich_text": [{"text": {"content": DRAFT_HEADING}}]},
        },
    ]

    inserted = 0
    slot_idx = -1
    buffer: list[str] = []

    def flush() -> None:
        """모아둔 줄을 문단 블록 하나로 만든다."""
        if not buffer:
            return
        text = "\n".join(buffer)
        buffer.clear()
        for chunk in _chunk_text(text):
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })

    for raw in draft.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush()
            continue

        if RE_IMAGE_SLOT.match(stripped):
            flush()
            slot_idx += 1
            img = images.get(slot_idx)
            if img:
                upload_id = upload_file(img["path"])
                if upload_id:
                    blocks.append(image_block(upload_id, img.get("caption", "")))
                    inserted += 1
                    continue
            # 이미지를 못 구했으면 자리 번호를 남긴다. 발행 담당자가 직접 넣는다.
            buffer.append(stripped)
            flush()
            continue

        buffer.append(line)

    flush()
    append_blocks(page_id, blocks)
    return inserted


# ── 선정 승격 / 보관 정책 정리 ──────────────────────────

def find_page_by_url(data_source_id: str, url: str) -> str | None:
    """URL로 페이지를 찾아 page_id 반환. 없으면 None."""
    body = {"filter": {"property": PROP_URL, "url": {"equals": url}}, "page_size": 1}
    data = _request("POST", f"/data_sources/{data_source_id}/query", json=body)
    results = data.get("results", [])
    return results[0]["id"] if results else None


def promote_to_selected(
    page_id: str,
    article,
    *,
    status: str = STATUS_DEFAULT,
    publish_mode: str = "",
) -> None:
    """이미 저장된 '수집됨' 페이지에 소주제·점수를 채우고 상태를 올린다.

    수집 단계에서 전체 기사를 먼저 저장하므로, 선정된 기사는 새로 만들지 않고
    기존 페이지를 갱신한다. (같은 기사가 두 번 저장되는 것을 막는다)

    status 를 인자로 받는 이유:
      소주제까지 만들었으면 '선정됨'(승인 대기)이지만, LLM을 건너뛴 경우
      소주제가 비어 있어 '선정요청'(소주제 생성 대기)이 맞다.
      비어 있는 채로 '선정됨'이 되면 publish가 집어가 빈 초안을 쓴다.
    """
    props: dict = {
        PROP_STATUS: {"select": {"name": status}},
    }
    # 점수 0은 '아직 계산되지 않음'을 뜻한다. 무조건 쓰면 collect 가 매긴
    # 점수를 subtopic 이 0으로 지워버린다. 값이 있을 때만 갱신한다.
    if article.score_norm:
        props[PROP_SCORE] = {"number": article.score_norm}
    # 발행구분은 선정 단계에서만 정해진다. 빈 값이면 기존 표시를 유지한다
    # (subtopic 이 collect 의 판정을 지우지 않게 한다).
    if publish_mode:
        props[PROP_MODE] = {"select": {"name": publish_mode}}
    if article.matched_keywords:
        kw = ", ".join(article.matched_keywords[:8])
        props[PROP_KEYWORDS] = {"rich_text": [{"text": {"content": kw[:MAX_TEXT_LEN]}}]}
    for name, text in zip(PROP_SUBTOPICS, article.subtopics):
        props[name] = {"rich_text": [{"text": {"content": text[:MAX_TEXT_LEN]}}]}
    _request("PATCH", f"/pages/{page_id}", json={"properties": props})


def update_status(page_id: str, status: str) -> None:
    """상태 속성만 바꾼다. 소주제·점수 등 다른 값은 건드리지 않는다.

    같은 사건으로 묶여 대표에게 자리를 내준 기사를 '수집됨'으로 되돌릴 때 쓴다.
    '선정요청'으로 두면 다음 subtopic 실행에서 또 후보로 올라와 무한히 반복된다.
    """
    props = {PROP_STATUS: {"select": {"name": status}}}
    _request("PATCH", f"/pages/{page_id}", json={"properties": props})


def fetch_by_status(data_source_id: str, status: str) -> list[dict]:
    """특정 상태의 페이지 목록. [{page_id, title, url, subtopics, created, bundle}]"""
    body = {
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
                    title = _plain_text(prop)
                    break
            subtopics = [
                _plain_text(props[name])
                for name in PROP_SUBTOPICS
                if name in props and _plain_text(props[name])
            ]
            bundle = (props.get(PROP_BUNDLE) or {}).get("select") or {}
            items.append({
                "page_id": page["id"],
                "title": title,
                "url": (props.get(PROP_URL) or {}).get("url", ""),
                "subtopics": subtopics,
                "created": page.get("created_time", ""),
                # 보드에서 사람이 옮긴 결과. confirm 이 이 값으로 묶음을 확정한다.
                "bundle": bundle.get("name", ""),
                # confirm 이 붙인 고유 이름. publish 가 이 값으로 묶음을 되살린다.
                "bundle_id": _plain_text(props.get(PROP_BUNDLE_ID, {})),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return items


def set_bundle(page_id: str, slot: str | None) -> None:
    """묶음 슬롯을 지정한다. None 이면 비운다 (보드의 '미배정' 칸으로 간다)."""
    value = {"select": {"name": slot}} if slot else {"select": None}
    _request("PATCH", f"/pages/{page_id}", json={"properties": {PROP_BUNDLE: value}})


def set_bundle_id(page_id: str, bundle_id: str | None) -> None:
    """확정된 묶음의 고유 이름을 붙인다. None 이면 비운다."""
    text = [{"text": {"content": bundle_id[:MAX_TEXT_LEN]}}] if bundle_id else []
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": {PROP_BUNDLE_ID: {"rich_text": text}}},
    )


def confirm_bundle(page_id: str, bundle_id: str) -> None:
    """확정 처리 — 묶음에 고유 이름을 붙인다.

    상태는 건드리지 않는다. 이미 '초안요청' 이고, publish 가 끝나야
    '작성완료' 로 간다. 묶음ID 가 붙었다는 것이 곧 '확정됨' 의 표시다.

    슬롯은 그대로 둔다. 확정했다고 카드가 보드에서 사라지면 무엇이
    초안을 기다리는 중인지 볼 수 없다. 슬롯은 초안이 나간 뒤
    finish_article() 이 비운다.

    둘을 한 번의 PATCH 로 보낸다. 나눠 보내면 중간에 끊겼을 때 묶음ID 는
    붙었는데 상태는 '선정됨'인 카드가 남아, 다음 확정에서 새 ID 를 받는다.
    """
    _request(
        "PATCH",
        f"/pages/{page_id}",
        json={
            "properties": {
                PROP_BUNDLE_ID: {
                    "rich_text": [{"text": {"content": bundle_id[:MAX_TEXT_LEN]}}]
                },
            }
        },
    )


def archive_page(page_id: str) -> None:
    """휴지통으로 이동 (Notion UI에서 30일간 복구 가능)."""
    _request("PATCH", f"/pages/{page_id}", json={"in_trash": True})


# '대기' 칸에서 짝을 기다리는 카드를 며칠까지 두고 볼지.
#
# confirm 이 거부 카드에 '⚠️ 승인 불가' 칩을 붙이던 시절에는 그 칩이
# 기준이었다. 칩은 2026-09-14 에 전부 걷어냈고, 지금 거부의 표시는
# 카드가 '대기' 칸에 놓였다는 사실 하나다.
#
#   실측(2026-09-02): 단독1·단독3 에 2건씩 몰려 거부됐고, 그 상태로
#   남아 두 칸이 묶였다. 지금은 거부 즉시 슬롯이 비워지므로 이 문제는
#   없지만, 짝이 안 채워지는 카드가 후보 목록에 계속 남는 문제는 남는다.
#
# 슬롯을 붙들지 않으므로 하루는 너무 짧다. 사흘이면 content 가 세 번
# 다시 묶어볼 기회를 준 것이고, 그래도 4건이 안 채워졌다면 그날의
# 뉴스로서 가치가 이미 떨어진 것이다.
STALE_WAIT_MAX_AGE_DAYS = 3


def cleanup_stale_wait(
    data_source_id: str,
    *,
    days: int = STALE_WAIT_MAX_AGE_DAYS,
    dry_run: bool = False,
) -> int:
    """'대기' 칸에서 N일 넘게 짝을 못 만난 카드를 '보류'로 넘긴다.

    cleanup_rejected() 의 후신이다. '대기' 칸을 기준으로 본다. 칸은
    카드가 실제로 놓인 자리라, 무엇이 방치됐는지를 정확히 말한다.

    기준은 페이지 생성 시각이다(수정 시각이 아니다). content 가 회차마다
    대기 카드를 다시 묶어보며 PATCH 를 날리므로, 수정 시각을 쓰면 카드가
    영원히 '방금 손댄 것'이 되어 아무것도 정리되지 않는다.

    지우지 않고 '보류'로 옮기는 이유
    -----------------------------
    판정이 틀렸을 수도 있고, 나중에 다시 쓸 수도 있다. '보류'로 두면
    Notion 에서 상태만 되돌려 살릴 수 있고, 그래도 손대지 않으면
    cleanup_expired 가 RETENTION_DAYS 에 따라 휴지통으로 보낸다.
    (휴지통 이동이라 그 뒤로도 30일간 복구할 수 있다)

    슬롯을 함께 비우는 이유
    ---------------------
    상태만 바꾸면 '보류'인데 '대기' 칸을 차지한 카드가 남는다. content 의
    점유 검사에는 걸리지 않지만, 나중에 상태를 되돌렸을 때 엉뚱한 칸에
    나타난다.
    """
    from datetime import datetime, timedelta

    from config.settings import KST

    cutoff = datetime.now(KST) - timedelta(days=days)
    targets = []

    for item in fetch_by_status(data_source_id, STATUS_DEFAULT):
        if item.get("bundle") != BUNDLE_WAIT:
            continue
        created = item.get("created")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.astimezone(KST) < cutoff:
            targets.append(item)

    if not targets:
        log.info(f"'{BUNDLE_WAIT}' 칸에 {days}일 이상 묵은 카드가 없습니다")
        return 0

    log.info(f"'{BUNDLE_WAIT}' {days}일 경과 {len(targets)}건 정리 대상")
    for item in targets[:5]:
        log.info(f"    - {item['title'][:50]}")
    if len(targets) > 5:
        log.info(f"    ... 외 {len(targets) - 5}건")

    if dry_run:
        return len(targets)

    ok = 0
    for item in targets:
        try:
            # 상태와 슬롯을 한 번의 PATCH 로 보낸다. 나눠 보내면 중간에
            # 끊겼을 때 '보류'인데 '대기' 칸을 붙들고 있는 카드가 남는다.
            _request(
                "PATCH",
                f"/pages/{item['page_id']}",
                json={
                    "properties": {
                        PROP_STATUS: {"select": {"name": STATUS_HOLD}},
                        PROP_BUNDLE: {"select": None},
                    }
                },
            )
            ok += 1
        except Exception as e:
            log.warning(f"정리 실패 ({item['title'][:30]}): {e}")

    log.info(f"묵은 대기 카드 {ok}/{len(targets)}건을 '{STATUS_HOLD}' 로 넘겼습니다")
    return ok


def cleanup_expired(data_source_id: str, *, dry_run: bool = False) -> dict[str, int]:
    """보관 기간이 지난 페이지를 휴지통으로 옮긴다.

    기준은 페이지 생성 시각(created_time). 상태별 보관일수는 config.RETENTION_DAYS.
    작성완료는 블로그 원고를 담고 있어 기본적으로 보존한다(KEEP_WRITTEN).
    완전 삭제가 아니라 휴지통 이동이므로 30일간 복구할 수 있다.
    """
    from datetime import datetime, timedelta

    from config.settings import KEEP_WRITTEN, KST, RETENTION_DAYS, WRITTEN_RETENTION_DAYS

    policy = dict(RETENTION_DAYS)
    if not KEEP_WRITTEN:
        policy[STATUS_WRITTEN] = WRITTEN_RETENTION_DAYS

    now = datetime.now(KST)
    removed: dict[str, int] = {}

    for status, days in policy.items():
        cutoff = now - timedelta(days=days)
        targets = []
        for item in fetch_by_status(data_source_id, status):
            created = item.get("created")
            if not created:
                continue
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if created_dt < cutoff:
                targets.append(item)

        if not targets:
            continue

        log.info(f"[{status}] {days}일 경과 {len(targets)}건 정리 대상")
        for item in targets[:5]:
            log.info(f"    - {item['title'][:50]}")
        if len(targets) > 5:
            log.info(f"    ... 외 {len(targets) - 5}건")

        if dry_run:
            removed[status] = len(targets)
            continue

        ok = 0
        for item in targets:
            try:
                archive_page(item["page_id"])
                ok += 1
            except Exception as e:
                log.warning(f"정리 실패 ({item['title'][:30]}): {e}")
        removed[status] = ok

    if removed:
        total = sum(removed.values())
        detail = ", ".join(f"{k} {v}건" for k, v in removed.items())
        log.info(f"보관 정리 완료: 총 {total}건 ({detail})" + (" [dry-run]" if dry_run else ""))
    else:
        log.info("보관 기간이 지난 페이지가 없습니다")
    return removed
"""스톡 사진을 먼저 찾고, 없을 때만 생성하는 삽화 에이전트.

    기사 분석 → Visual Plan ─┬─ 스톡 검색 성공 → 사진 사용
                             └─ 실패        → text2img 생성
                                              └─ 생성 실패 → 스톡 대체

왜 생성보다 스톡을 먼저 두는가:
  생성 이미지는 손발이 뭉개지고 도복이 가라테 gi 로 새는 문제가 남아 있다.
  실제 사진에는 그 문제가 없다. 라이선스가 명확한 스톡이 있다면 그쪽이 낫다.
  게다가 생성은 장당 수 초~수십 초다. 스톡으로 채운 자리만큼 시간이 준다.

검색어는 Visual Plan 의 scene 을 그대로 쓴다. 이미 영어 장면 묘사이고,
소제목보다 기사 맥락이 반영돼 있다.

생성이 불가능할 때:
  GPU 서버가 내려가 있으면(SSH 터널 끊김, 서버 미기동) 그 자리는 예전에는
  그냥 비었다. 실측(2026-09-02)으로 4편 16자리 중 1자리만 채워졌다.

  이제는 점수 하한 없이 남은 스톡 중 최선을 넣는다. 덜 맞는 사진이라도
  자리가 비는 것보다 낫다는 판단이다. 두 번에 나눠 고른다.

    1차  재사용 간격을 지키면서 하한만 푼다
    2차  1차로 못 채운 자리만, 재사용 간격까지 푼다

  하한만 풀어서는 부족했다. 스톡이 29장인데 재사용 간격이 14일이라
  최근 며칠만 돌려도 후보가 전부 제외 대상이 된다.

    실측(2026-09-02): 인덱스 29장이 모두 최근 7일 안에 쓰여 있었고,
    하한을 풀었는데도 4자리 전부 '쓸 스톡이 없습니다' 로 비었다.

  여유가 있으면 1차에서 끝나 사진이 겹치지 않고, 없으면 겹쳐서라도
  채운다. 한 글 안에서 같은 사진이 두 번 들어가는 일은 두 단계 모두에서
  막는다.

  어디까지나 안전망이다. 이 경로를 자주 타면 GPU 서버부터 확인하고,
  2차까지 내려간다면 스톡 장수가 모자란다는 뜻이다.

재사용 간격:
  스톡은 50장 남짓이라 매일 4장씩 쓰면 금방 돈다. 최근 STOCK_REUSE_DAYS 일
  안에 쓴 사진은 후보에서 뺀다.

프롬프트 로깅:
  삽화가 어긋났을 때 원인이 Plan 인지, 프롬프트 조립인지, 생성 모델인지를
  로그만으로 갈라야 한다. 셋 중 어디서 틀어졌는지 모르면 매번 코드를 다시
  읽게 된다. 그래서 GPU 로 보내는 positive/negative 를 자르지 않고 남긴다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image

from config.settings import BASE_DIR, DATA_DIR, IMAGE_DIR, KST, STATE_DIR
from core.logger import get_logger
from agents.illustration.article_analyzer import ArticleAnalysis, analyze
from agents.illustration.planned_image_agent import _hard_filter
from agents.illustration.prompt_compiler import (
    compile_negative,
    compile_prompt,
    render,
)
from agents.illustration.visual_planner import VisualPlan, plan_all
from tools.media_labeler import label

log = get_logger(__name__)

# build_index.py 가 만든 인덱스. 확장자는 붙이지 않는다.
STOCK_INDEX = DATA_DIR / "index"

# 사용 이력. {사진 경로: 마지막으로 쓴 날짜}
#
# data/ 가 아니라 state/ 에 둔다. data/ 는 .gitignore 로 빠지는데,
# GitHub Actions 는 실행마다 새 컨테이너라 거기 두면 재사용 간격이
# 매번 초기화돼 같은 사진이 날마다 나온다.
USAGE_LOG = STATE_DIR / "stock_usage.json"
_LEGACY_USAGE = DATA_DIR / "stock_usage.json"


def _migrate_usage() -> None:
    """예전 위치의 사용 이력을 state/ 로 한 번 옮긴다.

    옮기지 않으면 지난 14일 기록이 사라져 최근에 쓴 사진이 곧바로
    다시 후보에 오른다.
    """
    if USAGE_LOG.exists() or not _LEGACY_USAGE.exists():
        return
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        USAGE_LOG.write_text(
            _LEGACY_USAGE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        _LEGACY_USAGE.unlink()
        log.info(f"스톡 사용 이력을 {USAGE_LOG.name} 로 옮겼습니다")
    except OSError as e:
        log.warning(f"사용 이력 이전 실패(무시하고 진행): {e}")


_migrate_usage()

# 이 기간 안에 쓴 사진은 다시 쓰지 않는다.
STOCK_REUSE_DAYS = 14

# 스톡을 복사할 때 긴 변을 이 크기로 줄인다.
MAX_SIDE = 1600

_searcher = None
_searcher_failed = False


def _get_searcher():
    """ImageSearcher 를 한 번만 만들어 재사용한다.

    CLIP 로딩이 수 초 걸린다. 글마다 새로 만들면 그만큼 그대로 쌓인다.
    """
    global _searcher, _searcher_failed
    if _searcher is not None or _searcher_failed:
        return _searcher

    try:
        from tools.image_search import ImageSearcher

        _searcher = ImageSearcher(str(STOCK_INDEX))
        log.info(f"스톡 인덱스 {len(_searcher.paths)}장 로드")
    except FileNotFoundError as e:
        _searcher_failed = True
        log.warning(f"스톡 인덱스 없음 — 전부 생성으로 처리합니다: {e}")
    except Exception as e:
        _searcher_failed = True
        log.warning(f"스톡 검색을 쓸 수 없습니다: {type(e).__name__}: {e}")

    return _searcher


def _load_usage() -> dict[str, str]:
    if not USAGE_LOG.exists():
        return {}
    try:
        return json.loads(USAGE_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"사용 이력을 읽지 못했습니다: {e}")
        return {}


def _save_usage(usage: dict[str, str]) -> None:
    try:
        USAGE_LOG.write_text(
            json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning(f"사용 이력을 쓰지 못했습니다: {e}")


def _recently_used(usage: dict[str, str]) -> set[str]:
    """최근에 쓴 사진 경로. 날짜를 못 읽는 항목은 오래된 것으로 본다."""
    cutoff = datetime.now(KST) - timedelta(days=STOCK_REUSE_DAYS)
    recent = set()
    for path, iso in usage.items():
        try:
            dt = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        if dt > cutoff:
            recent.add(path)
    return recent


def _query_for(plan: VisualPlan, identifiers: list[str]) -> str:
    """Visual Plan → CLIP 검색어.

    생성 프롬프트와 달리 스타일·카메라 기술어를 넣지 않는다. CLIP 은 화풍도
    벡터에 반영하므로 'photorealistic, shallow depth of field' 같은 문구가
    피사체 신호를 희석시킨다. 장면만 남긴다.

    'a photo of' 로 시작하는 것은 CLIP 프롬프트의 관례다. 학습 데이터가
    캡션 문장이라 명사구보다 문장 형태에서 후보 간 점수 격차가 벌어진다.
    """
    scene, _ = _hard_filter(plan.scene, identifiers)
    return f"a photo of {scene}" if scene else ""


def _log_request(
    idx: int, total: int, heading: str, plan: VisualPlan,
    prompt: str, negative: str,
) -> None:
    """생성 요청 전문을 남긴다. 자르지 않는다."""
    log.info(f"┌ 삽화 {idx}/{total} 생성 요청 — {heading}")
    log.info(f"│ [plan] {plan.subject_count}인"
             f"{' (익명)' if getattr(plan, 'anonymous', False) else ''} "
             f"· {plan.shot} · {plan.composition} · {plan.aspect_ratio} "
             f"· 종목 {plan.domain}")
    log.info(f"│ [pos ] {prompt}")
    log.info(f"└ [neg ] {negative}")


# ── 출처 표기 ────────────────────────────────────────────────────────
#
# 라이선스상 표기 의무는 없다. Pexels License 도 CC0 도 출처를 밝히지
# 않아도 된다. 그래도 남기는 이유는 두 가지다.
#
#   1. 나중에 문의가 오면 원본을 대야 한다. 실제로 data/stock 에
#      유료 사진 18장이 섞여 있던 것을 뒤늦게 발견한 적이 있다.
#   2. 표기 구조가 있으면 CC BY 소스도 쓸 수 있다. 지금은 CC0 만
#      받지만 그 제약을 풀면 확보 가능한 사진이 몇 배로 늘어난다.
#
# Openverse 는 검색 엔진이지 사진의 출처가 아니다. 실제 제공자
# (Wikimedia Commons, Flickr)를 적는다.

_SOURCES_PATH = DATA_DIR / "stock_sources.json"
_sources_cache: dict | None = None

_PROVIDER_NAMES = {
    "wikimedia": "Wikimedia Commons",
    "flickr": "Flickr",
    "nasa": "NASA",
    "smithsonian": "Smithsonian",
}


def _load_sources() -> dict:
    global _sources_cache
    if _sources_cache is None:
        try:
            _sources_cache = json.loads(_SOURCES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _sources_cache = {}
    return _sources_cache


def _basename(path: str) -> str:
    """경로에서 파일명만 떼어낸다. Windows 구분자로 적혀 있어도 동작한다.

    Path('data\\stock\\x.jpg').name 은 Linux 에서 문자열 전체를 돌려준다.
    역슬래시가 경로 구분자가 아니기 때문이다. 그러면 stock_sources.json
    조회도, 파일명 규칙 추정도 모두 빗나가 출처 문구가 통째로 빈다.

        실측(2026-09-14 Actions): 삽화는 4장 다 들어갔는데 '출처: Pexels'
        가 한 줄도 안 붙었다. 사진은 제자리에 있었고 이름도 pexels- 로
        시작했는데, 비교 대상이 'data\\stock\\pexels-....jpg' 였다.

    _stock_path() 와 달리 파일이 실제로 있는지는 보지 않는다. 출처는
    이름만으로 판정하는 것이고, 원본이 지워진 뒤에도 답이 나와야 한다.
    """
    return Path(str(path).replace("\\", "/")).name


def _credit_from_name(name: str) -> str:
    """파일명으로 출처를 추정한다.

    stock_sources.json 이 생기기 전에 손으로 모은 사진들이 있다.
    Unsplash 9장이 여기 해당하는데, 파일명에 규칙이 남아 있어
    복원할 수 있다.
    """
    lower = name.lower()
    stem = Path(lower).stem
    if lower.startswith("pexels-"):
        return "Pexels"
    if lower.startswith("openverse-"):
        return "Openverse"
    if lower.startswith("photo-") or stem.endswith("-unsplash"):
        return "Unsplash"
    if lower.startswith("gemini_image") or lower.startswith("sdxl_"):
        return "AI 생성 이미지"
    return ""


def credit_for(generation: dict | None) -> str:
    """삽화 한 장의 출처 문구를 낸다. 알 수 없으면 빈 문자열.

    generation["original"] 이 스톡 원본 경로다. 발행용 사본은 슬러그로
    이름이 바뀌므로 이 값으로만 원본을 되짚을 수 있다.
    """
    if not generation:
        return ""
    if generation.get("source") == "generated":
        return "AI 생성 이미지"

    original = generation.get("original")
    if not original:
        return ""

    name = _basename(original)
    meta = _load_sources().get(name)
    if not meta:
        # 기록이 없는 사진. 파일명으로 추정하되, 그것도 안 되면
        # 빈 값을 낸다. 근거 없는 출처를 지어내지 않는다.
        return _credit_from_name(name)

    src = meta.get("source")
    if src == "pexels":
        return "Pexels"
    if src == "openverse":
        provider = (meta.get("provider") or "").lower()
        return _PROVIDER_NAMES.get(provider, "Openverse")
    if src in {"sdxl", "gemini"}:
        return "AI 생성 이미지"
    return _credit_from_name(name)


def credit_line(credits: list[str]) -> str:
    """글 하단에 넣을 한 줄. 중복을 없애고 순서를 지킨다."""
    stock, ai = [], False
    for c in credits:
        if not c:
            continue
        if c == "AI 생성 이미지":
            ai = True
        elif c not in stock:
            stock.append(c)

    if stock and ai:
        return f"이미지 출처: {', '.join(stock)} · 일부 삽화는 AI로 만들었어요."
    if stock:
        return f"이미지 출처: {', '.join(stock)}"
    if ai:
        return "삽화는 AI로 만들었어요."
    return ""


def _stock_path(src: str | Path) -> Path:
    """인덱스에 적힌 경로를 지금 이 OS 의 경로로 바꾼다.

    인덱스는 만들 때의 구분자를 그대로 적는다. Windows 에서 만들면
    'data\\stock\\x.jpg' 가 되는데, Linux 에서는 이게 경로가 아니라
    역슬래시가 든 파일명 하나로 읽혀 반드시 FileNotFoundError 가 난다.
    파일이 제자리에 있어도 소용없다.

        실측(2026-09-14 Actions): 스톡 매칭은 4자리 모두 성공했는데
        복사에서 전부 죽어 삽화 0장으로 끝났다.

    상대 경로는 BASE_DIR 기준으로 푼다. 지금은 러너의 작업 디렉터리가
    저장소 루트라 결과가 같지만, cwd 가 달라져도 깨지지 않게 명시해 둔다.

    구분자를 바꿔도 디렉터리가 맞는다는 보장은 없다. Windows 절대경로
    'C:/dev/.../x.jpg' 는 Linux 에서 절대경로가 아니라 BASE_DIR 밑으로
    붙어 버린다. 그래서 파일이 없으면 파일명만 떼어 정규 위치에서 한 번
    더 찾는다. 그래도 없으면 원래 경로를 돌려준다 — _copy_stock 의 실패
    로그가 '있어야 할 자리'를 가리켜야 원인을 짚을 수 있다.

    바꾸는 것은 파일을 여는 시점뿐이다. 재사용 이력(USAGE_LOG)의 키는
    인덱스에 적힌 문자열 그대로 둔다 — 키까지 정규화하면 기존 기록과
    맞지 않아 재사용 간격이 한 번 무력화된다.
    """
    path = Path(str(src).replace("\\", "/"))
    if not path.is_absolute():
        path = BASE_DIR / path
    if path.exists():
        return path

    alt = DATA_DIR / "stock" / path.name
    return alt if alt.exists() else path


def _copy_stock(src: Path, dest: Path) -> Path | None:
    """스톡 원본을 삽화 폴더로 복사한다.

    원본을 그대로 참조하지 않고 복사하는 이유:
      - avif/webp 는 Notion 이 받지 못한다. png 로 통일한다.
      - save_local 이 `../images/파일명` 으로 참조하므로 IMAGE_DIR 에 있어야 한다.
      - label() 이 캡션을 새겨 넣는다. 원본을 덮어쓰면 라이브러리가 오염된다.
    """
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            if max(img.size) > MAX_SIDE:
                ratio = MAX_SIDE / max(img.size)
                img = img.resize(
                    (round(img.width * ratio), round(img.height * ratio)),
                    Image.LANCZOS,
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(dest, format="PNG")
        return dest
    except Exception as e:
        log.warning(f"  스톡 복사 실패 ({src.name}): {type(e).__name__}: {e}")
        return None


def _fallback_pick(
    searcher,
    queries: list[str],
    recent: set[str],
    picked: set[str],
) -> list[tuple[str, float] | None]:
    """생성이 안 된 자리를 스톡으로 메운다. queries 와 같은 길이.

    1차는 재사용 간격을 지키고, 그래도 빈 자리만 2차에서 간격을 푼다.
    스톡이 넉넉하면 1차에서 끝나므로 최근에 쓴 사진이 다시 나오지 않는다.

    picked(이 글에서 이미 쓴 사진)는 두 단계 모두에서 제외한다. 한 편 안에
    같은 사진이 두 번 들어가면 대체가 아니라 결함으로 보인다.
    """
    def _assign(qs: list[str], exclude: set[str]):
        try:
            return searcher.assign_batch(qs, exclude=exclude, min_score=0.0)
        except Exception as e:
            log.warning(f"스톡 대체 실패: {type(e).__name__}: {e}")
            return [None] * len(qs)

    hits = _assign(queries, recent | picked)

    missing = [i for i, h in enumerate(hits) if h is None]
    if not missing:
        return hits

    # 2차 — 재사용 간격을 푼다. 1차에서 고른 것은 계속 제외한다.
    taken = picked | {h[0] for h in hits if h}
    second = _assign([queries[i] for i in missing], taken)

    got = sum(1 for h in second if h)
    if got:
        log.warning(
            f"재사용 간격({STOCK_REUSE_DAYS}일)을 풀어 {got}자리를 채웁니다 "
            f"— 최근에 쓴 사진이 다시 나올 수 있습니다"
        )
    for i, h in zip(missing, second):
        hits[i] = h
    return hits


def generate_for_sections(
    headings: list[str],
    slug: str,
    text_model: str,
    *,
    title: str = "",
    body: str = "",
) -> dict[int, dict]:
    """소제목별 삽화 1장씩. {인덱스: {"path", "caption", "alt"}}

    다른 삽화 에이전트와 반환 형식을 맞춘다. publish_workflow 는 형식만 안다.
    """
    if not headings:
        return {}

    from tools.diffusers_client import DiffusersUnavailable

    # 1단계 — 기사 분석 (기사 1건당 1회)
    # 분석이 실패해도 멈추지 않는다. 여기서 예외를 올리면 초안은 이미
    # 발행됐는데 삽화 때문에 실패 통지가 나간다.
    try:
        analysis: ArticleAnalysis = analyze(title, body, text_model)
    except Exception as e:
        log.warning(f"기사 분석 실패 — 기본값으로 진행합니다: {type(e).__name__}: {e}")
        analysis = ArticleAnalysis()

    identifiers = analysis.identifiers.all_terms()

    # 2단계 — 소제목마다 Visual Plan
    plans = plan_all(analysis, headings, text_model)

    # 3단계 — 스톡 일괄 배정.
    # 소제목마다 따로 검색하면 같은 사진이 두 칸에 걸린다. assign_batch 는
    # 확신이 높은 조합부터 확정해서 한 장이 한 칸에만 가도록 한다.
    searcher = _get_searcher()
    usage = _load_usage()
    picks: list[tuple[str, float] | None] = [None] * len(headings)

    if searcher:
        queries = [_query_for(p, identifiers) for p in plans]
        for i, q in enumerate(queries, 1):
            log.info(f"  스톡 질의 {i}/{len(queries)}: {q}")
        if all(queries):
            try:
                picks = searcher.assign_batch(queries, exclude=_recently_used(usage))
            except Exception as e:
                log.warning(f"스톡 검색 실패, 생성으로 넘어갑니다: {e}")
        else:
            log.warning("Visual Plan 의 scene 이 비어 스톡 검색을 건너뜁니다")

    # 4단계 — 자리마다 스톡 → 생성 순으로 확보한다.
    #   materials[idx] = (파일 경로, 생성 이력)
    #   need_fallback  = 생성이 불가능해 비어 버린 자리
    materials: dict[int, tuple[Path, dict]] = {}
    need_fallback: list[int] = []
    picked: set[str] = set()      # 이 글에서 이미 쓴 스톡 (같은 사진 두 번 금지)
    gpu_down = False

    today = datetime.now(KST).isoformat(timespec="seconds")
    used_stock = 0
    used_fallback = 0
    total = len(headings)

    for idx, (heading, plan) in enumerate(zip(headings, plans)):
        out = IMAGE_DIR / f"{slug}_{idx + 1}.png"
        pick = picks[idx]
        path = None
        generation: dict | None = None

        if pick:
            src, score = pick
            log.info(f"삽화 {idx + 1}/{total} 스톡 ({score:.3f}) — {heading[:24]}")
            log.info(f"  원본: {src}")
            path = _copy_stock(_stock_path(src), out)
            if path:
                generation = {
                    "source": "stock",
                    "prompt": _query_for(plan, identifiers),
                    "negative": "",
                    "score": round(score, 4),
                    "original": str(src),
                    "plan": plan.to_dict(),
                    "category": analysis.category,
                }
                usage[src] = today
                picked.add(src)
                used_stock += 1
            else:
                pick = None      # 복사 실패 — 아래 생성 경로로 떨어뜨린다

        if not pick:
            # GPU 가 이미 죽은 걸 확인했으면 다시 붙지 않는다.
            # 자리마다 연결을 시도하면 타임아웃이 그만큼 쌓인다.
            if gpu_down:
                need_fallback.append(idx)
                continue

            prompt = compile_prompt(plan, identifiers)
            negative = compile_negative(plan)
            _log_request(idx + 1, total, heading, plan, prompt, negative)
            try:
                path = render(plan, prompt, negative, out)
            except DiffusersUnavailable as e:
                log.warning(f"로컬 이미지 생성을 쓸 수 없습니다: {e}")
                log.warning("남은 자리는 스톡에서 채웁니다 (점수 하한 없음)")
                gpu_down = True
                need_fallback.append(idx)
                continue
            if not path:
                need_fallback.append(idx)
                continue
            generation = {
                "source": "generated",
                "prompt": prompt,
                "negative": negative,
                "plan": plan.to_dict(),
                "category": analysis.category,
            }

        materials[idx] = (path, generation)

    # 5단계 — 생성이 안 된 자리를 스톡으로 메운다.
    #
    # 1차 배정(assign_batch)은 MIN_SCORE 아래를 잘라낸다. 생성이라는
    # 대안이 있을 때는 그게 맞다. 그 대안이 사라졌으므로 여기서는
    # 하한 없이(min_score=0.0) 남은 것 중 최선을 가져온다.
    #
    # 재사용 간격은 1차에서만 지킨다(_fallback_pick). 처음에는 여기서도
    # 끝까지 지켰는데, 스톡 29장이 전부 최근 7일에 걸려 한 자리도 못 채웠다.
    if need_fallback and searcher:
        fb_queries = [
            _query_for(plans[i], identifiers) or "a photo of a taekwondo training hall"
            for i in need_fallback
        ]
        hits = _fallback_pick(searcher, fb_queries, _recently_used(usage), picked)

        for i, hit in zip(need_fallback, hits):
            if not hit:
                log.warning(
                    f"삽화 {i + 1}/{total} 자리를 채우지 못했습니다 "
                    f"— 인덱스에 쓸 사진이 없습니다 (현재 {len(searcher.paths)}장)"
                )
                continue
            src, score = hit
            out = IMAGE_DIR / f"{slug}_{i + 1}.png"
            path = _copy_stock(_stock_path(src), out)
            if not path:
                continue
            log.info(f"삽화 {i + 1}/{total} 스톡 대체 ({score:.3f}) — {headings[i][:24]}")
            log.info(f"  원본: {src}")
            usage[src] = today
            picked.add(src)
            used_fallback += 1
            materials[i] = (path, {
                "source": "stock_fallback",
                "prompt": _query_for(plans[i], identifiers),
                "negative": "",
                "score": round(score, 4),
                "original": str(src),
                "plan": plans[i].to_dict(),
                "category": analysis.category,
                "note": "생성 불가로 대체 (점수 하한 없음)",
            })
    elif need_fallback:
        log.warning(f"{len(need_fallback)}자리가 비었습니다 — 스톡 인덱스가 없어 채우지 못합니다")

    # 6단계 — 캡션을 새기고 반환 형식으로 정리한다
    result: dict[int, dict] = {}
    for idx in sorted(materials):
        path, generation = materials[idx]
        labeled = label(
            path,
            headings[idx],
            keywords=analysis.keywords,
            generation=generation,
            credit=credit_for(generation),
        )
        result[idx] = {
            "path": labeled.path,
            "caption": labeled.caption,
            "alt": labeled.alt_text,
            # 발행 쪽에서 쓰지 않으면 무시된다. 반환 계약을 깨지 않도록
            # 기존 키는 그대로 두고 더하기만 했다.
            "credit": credit_for(generation),
            "sidecar": labeled.sidecar,
        }

    if used_stock or used_fallback:
        _save_usage(usage)

    generated = len(result) - used_stock - used_fallback
    log.info(
        f"삽화 {len(result)}/{total}장 "
        f"(스톡 {used_stock} · 생성 {generated} · 스톡대체 {used_fallback}, "
        f"분류: {analysis.category})"
    )
    return result
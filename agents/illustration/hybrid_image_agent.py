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

from config.settings import DATA_DIR, IMAGE_DIR, KST, STATE_DIR
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
            path = _copy_stock(Path(src), out)
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
            path = _copy_stock(Path(src), out)
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
        )
        result[idx] = {
            "path": labeled.path,
            "caption": labeled.caption,
            "alt": labeled.alt_text,
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
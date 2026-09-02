"""1·2·8단계를 엮은 삽화 에이전트 (v3 2차).

    기사 분석 → Visual Plan → 프롬프트 → text2img → 표시·출력
      (1단계)     (2단계)      (조립부)              (8단계)

3~7단계(라이브러리 조회, 마스크, 인페인팅, 검수)는 아직 없다. 라이브러리
조회는 hybrid_image_agent 가 CLIP 검색으로 대신하고 있다.

--- 2차에서 고친 것 -----------------------------------------------------

1) 네거티브가 버려지고 있었다
   `_render` 가 인자로 받은 프롬프트는 쓰면서 네거티브는 **내부에서 다시
   계산**했다. 호출부가 plan.forbidden 을 합쳐 만든 값이 도달하지 못했고,
   로그에 찍힌 네거티브와 실제 전송된 네거티브가 서로 달랐다.
   'jersey numbers' 를 금지했는데 등번호가 찍힌 이미지가 나온 원인이다.
   → `_render(plan, prompt, out, negative=...)` 로 받는다.

2) 인물을 그리면 발차기가 불가능했다
   NEGATIVE_WITH_PEOPLE 이 feet / hands / fingers / full body 를 막고,
   compile_prompt 가 'upper body only' 를 강제했다. 손발 뭉개짐을 피하려던
   조치인데, 발차기·품새·격파가 전부 불가능해진다.
   → 신체를 금지하지 않고 **왜곡을 금지**한다(deformed hands, malformed
     feet, extra fingers). 관련 상수는 prompt_compiler 로 옮겼다.

3) 프롬프트가 77 토큰에서 잘렸다
   SPORT_CONSISTENCY 한 덩어리가 28 단어다. scene 과 STYLE 을 더하면
   130 토큰을 넘어 뒤가 통째로 버려졌고, 하필 STYLE(실사 지시)이 맨 끝에
   있었다. 삽화가 3D 렌더처럼 나온 원인이다.
   → 조립을 prompt_compiler 로 옮기고 우선순위 기반 예산 관리를 넣었다.

4) 조립부가 두 곳에 있었다
   이 모듈과 hybrid_image_agent 가 각각 프롬프트를 만들어 규칙이 갈렸다.
   → compile_prompt / negative_for / STYLE / NEGATIVE 를 여기서 **삭제**하고
     prompt_compiler 하나로 모았다. 이 모듈은 `_hard_filter` 와 `_render`
     만 제공한다.

고유명사 하드 필터가 이 모듈의 안전장치다. Plan 생성에 LLM 을 쓰는 이상,
격리했던 대회명이 scene 문자열로 새어 들어올 수 있다. 규칙 기반으로
마지막에 한 번 더 걸러낸다.
"""
from __future__ import annotations

import re
from pathlib import Path

from config.settings import IMAGE_DIR
from core.logger import get_logger
from agents.illustration.article_analyzer import ArticleAnalysis, analyze
from agents.illustration.visual_planner import VisualPlan, plan_all
from tools.media_labeler import label

log = get_logger(__name__)

# 종횡비 → 생성 해상도. 모델 계열마다 학습 해상도가 달라 표를 나눈다.
#
# SD1.5/turbo 계열은 512² 언저리, SDXL 계열은 1024² 언저리에서 학습됐다.
# SDXL 에 512급 크기를 넣으면 인체가 뭉개지고 구도가 무너진다. 반대로
# SD1.5 에 1024를 넣으면 인물이 중복 생성된다(머리 둘, 팔 넷).
# 값은 모두 8의 배수 — SD 계열의 요구 조건이다.
_SIZE_SD15 = {
    "16:9": (896, 512),
    "4:3": (832, 624),
    "1:1": (704, 704),
    "3:4": (624, 832),
}
_SIZE_SDXL = {
    "16:9": (1344, 768),
    "4:3": (1152, 896),
    "1:1": (1024, 1024),
    "3:4": (896, 1152),
}

_size_logged = False


def _size_for(aspect_ratio: str) -> tuple[int, int]:
    """설정된 모델에 맞는 해상도. 종횡비는 Visual Plan 이 확정한 값이다.

    판정을 모델 '이름' 으로 한다는 것이 이 함수의 약점이다. 원격 GPU 서버를
    쓰면서 DIFFUSERS_MODEL 에 'xl' 이 들어가지 않으면, 실제로는 SDXL 인데
    896x512 를 보내게 된다. 그 크기에서 SDXL 은 인체를 뭉갠다.

    조용히 틀리면 원인을 찾기 어려우므로 첫 호출에서 판정 결과를 남긴다.
    """
    global _size_logged
    from config.settings import DIFFUSERS_MODEL

    name = str(DIFFUSERS_MODEL).lower()
    is_xl = "xl" in name or "sdxl" in name or "playground" in name
    if not _size_logged:
        log.info(
            f"해상도 표: {'SDXL(1024급)' if is_xl else 'SD1.5(512급)'} "
            f"— DIFFUSERS_MODEL='{DIFFUSERS_MODEL}'"
        )
        if not is_xl:
            log.warning(
                "모델 이름에 'xl' 이 없어 512급 해상도를 씁니다. "
                "실제로 SDXL 을 쓰고 있다면 인체가 뭉개집니다 — "
                "DIFFUSERS_MODEL 값을 확인하세요."
            )
        _size_logged = True

    table = _SIZE_SDXL if is_xl else _SIZE_SD15
    return table.get(aspect_ratio, table["16:9"])


def _hard_filter(text: str, identifiers: list[str]) -> tuple[str, list[str]]:
    """격리한 고유명사가 프롬프트에 남아 있으면 지운다.

    LLM 에게 '쓰지 마세요' 라고 지시하는 것만으로는 확률적으로 샌다.
    규칙 기반 차단을 마지막에 둔다 (v3 5단계 명세).

    긴 항목부터 지운다. '김운용컵' 을 지우기 전에 '김운용' 을 지우면
    '컵' 이 남아 문장이 이상해진다.

    지운 뒤에는 파편을 정리한다. 'at the 김운용컵 in 무주' 에서 고유명사만
    빼면 'at the in ,' 이 남는데, 이런 조각이 프롬프트에 있으면 모델이
    문장을 잘못 해석한다.
    """
    removed: list[str] = []
    for term in identifiers:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        if pattern.search(text):
            text = pattern.sub("", text)
            removed.append(term)

    if removed:
        # 한 번만 돌리면 연쇄 파편이 남는다.
        # 'at the 김운용컵 in 무주' → 'at the in,' → 'in' 만 지워지고 'at the' 가 남는다.
        # 더 줄지 않을 때까지 반복한다 (안전을 위해 횟수를 제한).
        for _ in range(4):
            before = text
            text = re.sub(r"\s{2,}", " ", text)
            text = re.sub(r"\s+([,.])", r"\1", text)   # 부호 앞 공백
            # 목적어를 잃은 전치사구: 'at the,' 'in,' 'during the.'
            text = re.sub(
                r"\b(?:at|in|on|during|from|of|near|by)(?:\s+the)?(?=\s*[,.]|\s*$)",
                "", text, flags=re.I,
            )
            text = re.sub(r"(?:\s*,){2,}", ",", text)  # 연속 쉼표
            text = text.strip(" ,.")
            if text == before:
                break

    return text.strip(" ,."), removed


def _render(
    plan: VisualPlan,
    prompt: str,
    out: Path,
    negative: str | None = None,
) -> Path | None:
    """실제 이미지 생성. 3~7단계가 들어오면 이 함수만 교체된다.

    negative 를 인자로 받는다. 이전에는 내부에서 다시 계산했는데, 그러면
    호출부가 이 Plan 을 위해 만든 금지어가 도달하지 못한다. 로그에 찍히는
    값과 실제 전송되는 값이 달라져 원인을 추적할 수 없게 된다.

    negative 가 None 이면 조립부의 기본값을 만들어 쓴다. 지연 import 인
    이유는 prompt_compiler 가 이 모듈의 `_hard_filter` 를 참조하기 때문이다
    (모듈 최상단에서 서로 import 하면 순환한다).
    """
    from tools.diffusers_client import DiffusersUnavailable, generate

    if negative is None:
        from agents.illustration.prompt_compiler import compile_negative

        negative = compile_negative(plan)

    width, height = _size_for(plan.aspect_ratio)
    try:
        return generate(prompt, negative, out, width=width, height=height)
    except DiffusersUnavailable:
        raise
    except Exception as e:
        log.warning(f"  생성 실패: {e}")
        return None


def generate_for_sections(
    headings: list[str],
    slug: str,
    text_model: str,
    *,
    title: str = "",
    body: str = "",
) -> dict[int, dict]:
    """소제목별 삽화 1장씩. {인덱스: {"path", "caption", "alt"}}

    스톡 없이 전부 생성하는 경로다. 스톡을 먼저 쓰려면
    hybrid_image_agent.generate_for_sections 를 쓴다.

    기존 두 에이전트와 반환 형식을 맞춘다. publish_workflow 는 형식만 알면 된다.
    """
    if not headings:
        return {}

    from tools.diffusers_client import DiffusersUnavailable
    from agents.illustration.prompt_compiler import compile_negative, compile_prompt

    # 1단계 — 기사 분석 (기사 1건당 1회)
    analysis: ArticleAnalysis = analyze(title, body, text_model)
    identifiers = analysis.identifiers.all_terms()

    # 2단계 — 소제목마다 Visual Plan
    plans = plan_all(analysis, headings, text_model)

    result: dict[int, dict] = {}
    total = len(headings)

    for idx, (heading, plan) in enumerate(zip(headings, plans)):
        prompt = compile_prompt(plan, identifiers)
        negative = compile_negative(plan)
        out = IMAGE_DIR / f"{slug}_{idx + 1}.png"

        log.info(f"┌ 삽화 {idx + 1}/{total} 생성 요청 — {heading}")
        log.info(f"│ [pos ] {prompt}")
        log.info(f"└ [neg ] {negative}")

        try:
            path = _render(plan, prompt, out, negative=negative)
        except DiffusersUnavailable as e:
            log.warning(f"로컬 이미지 생성을 쓸 수 없습니다: {e}")
            break
        if not path:
            continue

        # 8단계 — 표시 및 출력
        labeled = label(
            path,
            heading,
            keywords=analysis.keywords,
            generation={
                "source": "generated",
                "prompt": prompt,
                "negative": negative,
                "plan": plan.to_dict(),
                "category": analysis.category,
            },
        )
        result[idx] = {
            "path": labeled.path,
            "caption": labeled.caption,
            "alt": labeled.alt_text,
        }

    log.info(f"삽화 {len(result)}/{total}장 생성 (분류: {analysis.category})")
    return result

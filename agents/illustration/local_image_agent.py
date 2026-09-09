"""로컬 삽화 생성. 소제목 → 영어 프롬프트 → diffusers.

generated_image_agent(Gemini)와 인터페이스를 맞춘다:
    generate_for_sections(headings, slug) -> {섹션 인덱스: {"path", "caption"}}

LLM을 쓰지 않는다:
  Gemini 경로는 소제목을 LLM에 넣어 영어 시각 개념을 뽑았다. 그런데 그
  텍스트 모델이 무료 티어 일일 한도에 걸리면(실측) 이미지 모델이 멀쩡해도
  삽화가 0장이 됐다. 로컬 생성으로 옮기는 목적이 할당량 탈출인데 앞단에
  API 의존이 남으면 의미가 없다. 그래서 키워드 사전으로 장면을 고른다.

  대신 프롬프트가 단순해진다. 소제목의 뉘앙스를 살리지 못하고 장면 유형만
  맞춘다. 삽화는 본문을 보조하는 역할이므로 이 정도로 충분하다고 본다.

Stable Diffusion 특성:
  - 한국어 프롬프트를 이해하지 못한다. 영어로 변환해야 한다.
  - 프롬프트 안의 부정 지시("no text")를 잘 따르지 않는다.
    negative_prompt 로 따로 넘겨야 한다.
  - 사람의 팔다리를 자주 망친다. 발차기 같은 동작은 특히 취약하다.
    평면 벡터 스타일로 추상화하면 눈에 덜 띈다.
"""

from config.settings import IMAGE_DIR
from core.logger import get_logger
from tools.diffusers_client import DiffusersUnavailable, generate

log = get_logger(__name__)

# 소제목에 이 단어가 있으면 해당 장면을 쓴다. 위에서부터 먼저 맞는 것을 채택하므로
# 구체적인 것(시상식)을 일반적인 것(대회)보다 앞에 둔다.
SCENE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("시상", "메달", "우승", "입상", "트로피"),
     "an awards podium with medals and a trophy, celebration"),
    (("퍼레이드", "개막", "축제", "행진", "한마당"),
     "a festive outdoor parade with flags and crowds, celebration banners"),
    (("공연", "시범", "격파", "태권체조"),
     "a martial arts demonstration on a stage, dynamic silhouettes, spotlights"),
    (("품새", "겨루기", "경기", "대회", "선수권", "그랑프리"),
     "a martial arts competition in an indoor arena, mats and scoreboards"),
    (("장학", "기부", "후원", "협약", "MOU", "간담"),
     "a formal ceremony with people shaking hands, banner backdrop"),
    (("교육", "세미나", "학술", "연수", "강습"),
     "a seminar room with a presenter and seated audience"),
    (("체육관", "시설", "보수", "개관", "센터"),
     "a clean modern gymnasium interior, wide empty floor"),
    (("판결", "실형", "협회", "논란", "공정", "심사"),
     "an abstract scene of scales of justice and documents on a desk"),
    (("AI", "기술", "가상", "버추얼", "VR", "디지털"),
     "abstract digital technology motifs, circuits and glowing lines"),
    (("어린이", "수련생", "학부모", "도장", "승급"),
     "children in martial arts uniforms training in a dojang, seen from behind"),
]

DEFAULT_SCENE = "a martial arts training hall with taekwondo motifs, calm composition"

# 모든 프롬프트에 붙는 스타일. 화풍을 통일해 글 전체가 한 세트로 보이게 한다.
STYLE = (
    "flat vector illustration, minimal geometric shapes, clean lines, "
    "limited palette of deep blue, white and red accents, "
    "stylized and abstract, non-photographic, editorial illustration"
)

# 부정 프롬프트. SD는 프롬프트 안의 "no ..." 를 잘 안 듣기 때문에 따로 넘긴다.
# 글자·로고·국기를 막는 이유: 블로그에 그대로 실리므로 잘못된 글자가 눈에 띈다.
# 얼굴을 막는 이유: 실존 인물처럼 보이면 초상권 오해를 살 수 있다.
NEGATIVE = (
    "text, letters, words, watermark, signature, logo, national flag, "
    "photorealistic, photograph, realistic face, portrait, close-up face, "
    "deformed hands, extra limbs, distorted anatomy, blurry, low quality"
)


def scene_for(heading: str) -> str:
    """소제목 → 영어 장면 묘사. 맞는 규칙이 없으면 기본 장면."""
    for keywords, scene in SCENE_RULES:
        if any(k in heading for k in keywords):
            return scene
    return DEFAULT_SCENE


def build_prompt(heading: str) -> str:
    """소제목 하나에 대한 최종 프롬프트."""
    return f"{scene_for(heading)}, {STYLE}"


def generate_for_sections(headings: list[str], slug: str) -> dict[int, dict]:
    """소제목별 삽화 1장씩 생성해 파일로 저장.

    {섹션 인덱스: {"path": Path, "caption": str}}  (Gemini 경로와 형식 통일)

    CPU에서는 장당 수십 초~수 분이 걸린다. 중간에 실패해도 그때까지 만든
    것은 그대로 반환한다 — 삽화 3장이라도 붙이는 편이 0장보다 낫다.
    """
    if not headings:
        return {}

    result: dict[int, dict] = {}
    for idx, heading in enumerate(headings):
        prompt = build_prompt(heading)
        out = IMAGE_DIR / f"{slug}_{idx + 1}.png"
        log.info(f"삽화 {idx + 1}/{len(headings)} 생성 중… ({heading[:24]})")
        try:
            path = generate(prompt, NEGATIVE, out)
        except DiffusersUnavailable as e:
            log.warning(f"로컬 이미지 생성을 쓸 수 없습니다: {e}")
            break
        if path:
            result[idx] = {"path": path, "caption": heading[:80]}

    log.info(f"삽화 {len(result)}/{len(headings)}장 생성")
    return result

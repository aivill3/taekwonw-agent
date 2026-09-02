"""텍스트 정규화 — 마크다운 제거, 매칭용 정규화, 문맥 발췌.

금칙어 검사는 '표기 흔들림'을 넘어야 한다. 같은 말이 '최고!!', '최~고',
'최 고' 처럼 적히면 단순 문자열 비교로는 못 잡는다. 그렇다고 정규화한
텍스트에서 찾은 위치를 그대로 쓰면, 사람에게 보여줄 원문 위치와 어긋난다.

그래서 normalize_with_map() 은 정규화 결과와 함께 '원문 인덱스 지도'를 준다.
지도의 i번째 값은 정규화 텍스트의 i번째 글자가 원문 어디서 왔는지다.
검사는 정규화본에서 하고, 보고는 원문 좌표로 한다.
"""
import re
import unicodedata

# 마크다운 문법 (draft_postprocessor 의 잔재 제거와는 목적이 다르다.
# 이쪽은 '검사·색인용 평문'을 만드는 것이고, 그쪽은 발행물을 다듬는 것이다)
_RE_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_RE_INLINE_CODE = re.compile(r"`([^`]*)`")
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_RE_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_RE_LIST = re.compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s+", re.M)
_RE_HR = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.M)
_RE_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|~~)(.+?)\1", re.S)
_RE_MULTINEWLINE = re.compile(r"\n{3,}")

# 매칭용 정규화에서 버릴 문자: 공백과 기호. 한글·영숫자만 남긴다.
_RE_DROPPABLE = re.compile(r"[\s\W_]", re.U)


def strip_markdown(text: str) -> str:
    """마크다운 문법을 걷어내고 평문만 남긴다. 링크·강조는 안쪽 글자를 보존한다.

    글자 수와 문장 수를 세는 쪽에서 쓰므로, 표기 기호가 분량으로 잡히면 안 된다.
    """
    if not isinstance(text, str) or not text:
        return ""
    text = _RE_CODE_BLOCK.sub("", text)
    text = _RE_IMAGE.sub("", text)
    text = _RE_LINK.sub(r"\1", text)      # [표시]( 링크 ) → 표시
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_HR.sub("", text)
    text = _RE_HEADING.sub("", text)
    text = _RE_QUOTE.sub("", text)
    text = _RE_LIST.sub("", text)
    text = _RE_EMPHASIS.sub(r"\2", text)  # **굵게** → 굵게
    text = _RE_MULTINEWLINE.sub("\n\n", text)
    return text.strip()


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """매칭용으로 정규화하고, 원문 인덱스 지도를 함께 돌려준다.

    반환 (normalized, index_map):
      normalized[i] 는 원문의 index_map[i] 번째 글자에서 왔다.

    하는 일:
      - 유니코드 NFKC 정규화 (전각→반각, 호환 문자 통일)
      - 공백·문장부호 제거 ('최 고!!' → '최고')
      - 소문자화 (영문 금칙어 대응)

    지도가 필요한 이유:
      정규화본에서 찾은 위치를 그대로 보고하면 '최 고!!' 에서 잡은 것을
      원문 2~3번째 글자라고 알려주게 된다. 사람이 그 위치를 열어보면
      엉뚱한 글자가 있다. 검사는 정규화본에서, 보고는 원문 좌표로 한다.
    """
    if not isinstance(text, str) or not text:
        return "", []

    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        folded = unicodedata.normalize("NFKC", ch)
        for sub in folded:
            if _RE_DROPPABLE.match(sub):
                continue
            chars.append(sub.lower())
            # 확장된 글자들도 모두 '원문의 i번째'에서 왔다고 기록한다
            index_map.append(i)
    return "".join(chars), index_map


def make_context(text: str, start: int, end: int, window: int = 20) -> str:
    """적발 지점 앞뒤를 잘라 사람이 판단할 문맥을 만든다.

    잘린 쪽에는 줄임표를 붙여, 문장이 원래 거기서 끝난 것처럼 보이지 않게 한다.
    검토자가 '이게 정말 금칙어인가'를 판단하려면 앞뒤가 필요하다.
    """
    if not isinstance(text, str) or not text:
        return ""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))

    left = max(0, start - window)
    right = min(len(text), end + window)
    snippet = text[left:right].replace("\n", " ").strip()

    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet = snippet + "…"
    return snippet

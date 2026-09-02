"""제목 규칙 검증 및 자동 교정.

LLM은 "고유명사 3개 이하" 같은 개수 제약을 자주 어긴다.
프롬프트로만 걸지 말고 후처리에서 결정론적으로 잡는다.

사용
    from title_rules import check_title, fix_title

    verdict = check_title(title, kind="bundle")
    if not verdict.ok:
        title = fix_title(title, kind="bundle")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_LEN = 32
MAX_PROPER_NOUNS = 3          # 묶음글 나열 항목 상한
LEAD_TOKEN = "태권도"          # 묶음글 선두 고정 토큰
TAIL_TOKENS = ("총정리", "정리")

# 서술어 종결로 인정하는 어미. 본문과 같은 해요체를 기본으로 한다.
_VERB_ENDING = re.compile(
    r"(어요|에요|예요|아요|해요|났어요|됐어요|열려요|올랐어요)[.?!]?$"
)

# 명사형 종결 — 단독글에서 금지
_NOUN_ENDING = re.compile(r"(개최|성과|현황|소식|결과|정리|총정리|참가|수상)$")

_BANNED_CHARS = re.compile(r"[!\[\]【】★☆♥♡▶◀]")

# 붙여쓰기 교정 대상. 지명·기관명 뒤에 꼬리 토큰이 바로 붙은 경우.
_GLUED_TAIL = re.compile(r"(?<=[가-힣])(총정리|정리)$")


@dataclass
class Verdict:
    ok: bool
    kind: str
    issues: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:          # if verdict: 로 쓸 수 있게
        return self.ok


def _tokens(title: str) -> list[str]:
    return [t for t in title.strip().split() if t]


def _proper_nouns(title: str) -> list[str]:
    """선두 `태권도`와 꼬리 `총정리`를 뺀 나열 항목."""
    toks = _tokens(title)
    if toks and toks[0] == LEAD_TOKEN:
        toks = toks[1:]
    if toks and toks[-1] in TAIL_TOKENS:
        toks = toks[:-1]
    return toks


def check_title(title: str, kind: str) -> Verdict:
    """kind: 'single'(단독글) | 'bundle'(묶음글)"""
    title = (title or "").strip()
    issues: list[str] = []

    if not title:
        return Verdict(False, kind, ["제목이 비어 있음"])

    if len(title) > MAX_LEN:
        issues.append(f"길이 {len(title)}자 — {MAX_LEN}자 초과")
    if _BANNED_CHARS.search(title):
        issues.append("금지 문자(느낌표·대괄호·이모지) 포함")

    if kind == "single":
        if not _VERB_ENDING.search(title):
            issues.append("서술어로 끝나지 않음 — 동사구형 위반")
        if _NOUN_ENDING.search(title):
            issues.append("명사형 종결 — 단독글에서 금지")
        if "?" in title:
            issues.append("물음표는 묶음글에서만 허용")

    elif kind == "bundle":
        toks = _tokens(title)
        if not toks or toks[0] != LEAD_TOKEN:
            issues.append(f"선두 토큰이 '{LEAD_TOKEN}'이 아님")
        if _GLUED_TAIL.search(title):
            issues.append("꼬리 토큰이 앞 단어에 붙어 있음 (예: 무주총정리)")
        n = len(_proper_nouns(title))
        if n > MAX_PROPER_NOUNS:
            issues.append(f"나열 항목 {n}개 — 최대 {MAX_PROPER_NOUNS}개")
        if n == 0:
            issues.append("나열 항목이 없음")

    else:
        issues.append(f"알 수 없는 kind: {kind}")

    return Verdict(not issues, kind, issues)


def fix_title(title: str, kind: str, priority: list[str] | None = None) -> str:
    """기계적으로 고칠 수 있는 위반만 교정한다.

    교정 가능: 붙여쓰기, 선두 토큰 누락, 나열 항목 초과, 금지 문자
    교정 불가: 서술어 누락 — 재생성이 필요하므로 원문을 그대로 돌려준다.

    priority: 남길 고유명사 우선순위. 앞쪽이 우선.
              생략하면 제목에 등장한 순서를 따른다.
    """
    title = _BANNED_CHARS.sub("", (title or "")).strip()
    if kind != "bundle":
        return re.sub(r"\s+", " ", title).strip()

    # 붙여쓰기 분리: 무주총정리 -> 무주 총정리
    title = _GLUED_TAIL.sub(r" \1", title)

    toks = _tokens(title)
    tail = toks[-1] if toks and toks[-1] in TAIL_TOKENS else TAIL_TOKENS[0]
    nouns = _proper_nouns(" ".join(toks))

    if priority:
        rank = {w: i for i, w in enumerate(priority)}
        nouns.sort(key=lambda w: rank.get(w, len(rank)))
    nouns = nouns[:MAX_PROPER_NOUNS]

    fixed = " ".join([LEAD_TOKEN, *nouns, tail])
    return re.sub(r"\s+", " ", fixed).strip()


if __name__ == "__main__":
    samples = [
        ("태권도 독도 서진주 대구 무주총정리", "bundle"),
        ("태권도 신성대 예산중 구미제일고 메달 정리", "bundle"),
        ("독도에서 광복절 태권도 시범이 펼쳐졌어요", "single"),
        ("구미제일고 태권도부 전국대회 성과", "single"),
    ]
    for t, k in samples:
        v = check_title(t, k)
        print(f"[{k}] {t}")
        print(f"  통과: {v.ok}")
        for i in v.issues:
            print(f"  - {i}")
        if not v.ok and k == "bundle":
            print(f"  교정: {fix_title(t, k, priority=['독도', '대구', '무주'])}")
        print()
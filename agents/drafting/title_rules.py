"""제목 규칙 검증 및 자동 교정.

제목은 글 유형(단독글/묶음글)과 무관하게 하나의 규칙을 따른다:
본문 전체를 함축한 17~18자 의문문. LLM은 길이와 의문문 종결을 자주
어기므로, 프롬프트로만 걸지 말고 후처리에서 결정론적으로 잡는다.

사용
    from title_rules import check_title, fix_title

    verdict = check_title(title)
    if not verdict.ok:
        title = fix_title(title)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE_MIN = 17
TITLE_MAX = 18

_BANNED_CHARS = re.compile(r"[!\[\]【】★☆♥♡▶◀]")
_QUESTION_ENDING = re.compile(r"\?$")


@dataclass
class Verdict:
    ok: bool
    issues: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:          # if verdict: 로 쓸 수 있게
        return self.ok


def check_title(title: str) -> Verdict:
    title = (title or "").strip()
    issues: list[str] = []

    if not title:
        return Verdict(False, ["제목이 비어 있음"])

    length = len(title)
    if length < TITLE_MIN:
        issues.append(f"길이 {length}자 — 최소 {TITLE_MIN}자")
    elif length > TITLE_MAX:
        issues.append(f"길이 {length}자 — 최대 {TITLE_MAX}자")

    if _BANNED_CHARS.search(title):
        issues.append("금지 문자(느낌표·대괄호·이모지) 포함")

    if not _QUESTION_ENDING.search(title):
        issues.append("의문문으로 끝나지 않음 — 물음표(?) 필요")

    return Verdict(not issues, issues)


def fix_title(title: str) -> str:
    """기계적으로 고칠 수 있는 위반만 교정한다.

    교정 가능: 금지 문자 제거, 공백 정리
    교정 불가: 길이(17~18자)·의문문 종결 — 문장을 다시 지어야 하므로
               재생성이 필요하다. 이 두 위반은 그대로 남는다.
    """
    title = _BANNED_CHARS.sub("", (title or "")).strip()
    return re.sub(r"\s+", " ", title).strip()
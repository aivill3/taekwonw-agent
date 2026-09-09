"""초안 정규화 후처리.

LLM이 반복해서 못 지키는 규칙을 코드가 결정론적으로 고친다.

왜 프롬프트가 아니라 코드인가
----------------------------
챕터 번호는 프롬프트에 예시 3개를 넣고 "숫자를 빠뜨리면 안 됩니다"라고
강조해도 세 번 연속 누락됐다. 반면 규칙 자체는 완전히 기계적이다.
`첫 번째는` 앞에 `1.`을 붙이는 데 판단이 필요하지 않다.

이런 규칙은 LLM에게 맡길 이유가 없다. 프롬프트에서 빼면 지시가 짧아져
남은 규칙의 준수율도 올라간다.

무엇을 고치고 무엇을 고치지 않는가
--------------------------------
고친다   형식 문제. 원문의 의미를 바꾸지 않는 치환
고치지 않는다   사실관계, 문장 구성, 분량

사실이 틀렸으면 재생성해야지 후처리로 가릴 일이 아니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.text_metrics import load_cta  # noqa: F401  (재수출 — 아래 설명 참조)

# 순서 표현 -> 번호
_ORDINAL_NUM = {
    "첫": 1, "두": 2, "세": 3, "네": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8,
}

# 번호 없이 순서만 쓴 챕터 줄
_ORDINAL_LINE = re.compile(
    r"^(?P<ord>첫|두|세|네|다섯|여섯|일곱|여덟)\s*번째(?P<rest>는|로|으로)?"
)

# 이미 번호가 붙은 챕터 줄
_NUMBERED_LINE = re.compile(r"^\s*\d{1,2}\s*[.)]\s*")
# `1. 첫 번째는 ...` — 숫자 뒤에 서수 표현이 오는 챕터 줄만 매칭한다.
_NUMBERED_ORDINAL = re.compile(
    r"^\s*\d{1,2}\s*[.)]\s*(?P<rest>(?:첫|두|세|네|다섯|여섯|일곱|여덟)\s*번째는.*)$"
)

# 해요체로 바꿀 종결어미. (정규식, 치환) 순.
# '~답니다'는 합니다체로 분류되어 문체 일관성을 떨어뜨린다.
_STYLE_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"했답니다"), "했어요"),
    (re.compile(r"됐답니다"), "됐어요"),
    (re.compile(r"였답니다"), "였어요"),
    (re.compile(r"이었답니다"), "이었어요"),
    (re.compile(r"([가-힣])았답니다"), r"\1았어요"),
    (re.compile(r"([가-힣])었답니다"), r"\1었어요"),
    (re.compile(r"([가-힣])랍니다"), r"\1래요"),
    (re.compile(r"([가-힣])ᆫ답니다"), r"\1대요"),
    (re.compile(r"([가-힣])답니다"), r"\1어요"),
]

# 마크다운 잔재. 네이버 에디터에서는 기호가 그대로 노출된다.
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_BULLET = re.compile(r"^(\s{0,3})[-*•]\s+", re.MULTILINE)

# 블록 마커 `/* 소제목 */` 는 마크다운이 아니라 발행용 서식 표시다.
# _MD_ITALIC 이 `* 본문 *` 부분을 기울임으로 오인해 별표를 지워버리므로
# (실측: `/* 본문 */` -> `/ 본문 /`) 제거 작업 동안 잠시 치환해 보호한다.
_MARKER = re.compile(r"/\*\s*(소제목|본문|인용구)\s*\*/")
_MARKER_SENTINEL = "\x00MARK{}\x00"

_ZWSP = "\u200b"


@dataclass
class PostProcessResult:
    """후처리 결과와 무엇을 고쳤는지 기록."""

    text: str
    chapters_numbered: int = 0
    style_fixes: int = 0
    markdown_removed: int = 0
    cta_appended: bool = False
    changes: list[str] = field(default_factory=list)

    @property
    def total_fixes(self) -> int:
        return self.chapters_numbered + self.style_fixes + self.markdown_removed

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapters_numbered": self.chapters_numbered,
            "style_fixes": self.style_fixes,
            "markdown_removed": self.markdown_removed,
            "cta_appended": self.cta_appended,
            "total_fixes": self.total_fixes,
            "changes": self.changes,
        }


def number_chapters(text: str) -> tuple[str, int, list[str]]:
    """챕터 줄 앞의 아라비아 숫자를 제거한다.

    이름과 반대되는 동작을 한다. 과거에는 `1. 첫 번째는` 처럼 번호를 붙였으나,
    실측 결과 이미지 자리 번호(숫자 한 줄)와 섞여 읽기 어려웠다.
    지금은 챕터를 `첫 번째는` 으로 열고 숫자는 이미지 자리에만 쓴다.

    호출부(postprocess)와 테스트가 이 이름을 참조하므로 이름은 유지한다.
    """
    lines = text.split("\n")
    fixed = 0
    changes: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.replace(_ZWSP, "").strip()
        if not stripped:
            continue
        # 서수 표현이 뒤따르는 경우만 건드린다. 표 안의 숫자나
        # 이미지 자리 번호(숫자만 있는 줄)를 지우면 안 된다.
        m = _NUMBERED_ORDINAL.match(stripped)
        if not m:
            continue
        lines[i] = m.group("rest")
        fixed += 1
        changes.append(f"챕터 번호 제거: '{stripped[:20]}'")

    return "\n".join(lines), fixed, changes


def fix_style(text: str) -> tuple[str, int, list[str]]:
    """합니다체로 분류되는 종결어미를 해요체로 바꾼다."""
    fixed = 0
    changes: list[str] = []
    for pattern, repl in _STYLE_FIXES:
        matches = pattern.findall(text)
        if not matches:
            continue
        before_sample = pattern.search(text)
        text = pattern.sub(repl, text)
        fixed += len(matches)
        if before_sample:
            changes.append(
                f"문체 교정 {len(matches)}건: '{before_sample.group(0)}' 계열"
            )
    return text, fixed, changes


def strip_markdown_residue(text: str) -> tuple[str, int, list[str]]:
    """네이버 에디터에서 그대로 노출되는 마크다운 기호를 제거한다.

    헤딩과 불릿은 기호만 지우고 내용은 남긴다. 챕터 번호 부여보다
    먼저 실행해야 `## 첫 번째는`도 번호를 받을 수 있다.
    """
    changes: list[str] = []
    count = 0

    # 마커를 잠시 걷어낸다 (별표가 마크다운으로 오인되는 것을 막는다)
    saved: list[str] = []

    def _hide(m: re.Match) -> str:
        saved.append(m.group(0))
        return _MARKER_SENTINEL.format(len(saved) - 1)

    text = _MARKER.sub(_hide, text)

    n = len(_MD_HEADING.findall(text))
    if n:
        text = _MD_HEADING.sub("", text)
        count += n
        changes.append(f"마크다운 헤딩 {n}개 제거")

    n = len(_MD_BULLET.findall(text))
    if n:
        text = _MD_BULLET.sub(r"\1", text)
        count += n
        changes.append(f"불릿 기호 {n}개 제거")

    n = len(_MD_BOLD.findall(text))
    if n:
        text = _MD_BOLD.sub(r"\1", text)
        count += n
        changes.append(f"굵게 표시 {n}개 제거")

    n = len(_MD_ITALIC.findall(text))
    if n:
        text = _MD_ITALIC.sub(r"\1", text)
        count += n
        changes.append(f"기울임 표시 {n}개 제거")

    # 마커 복원
    for i, original in enumerate(saved):
        text = text.replace(_MARKER_SENTINEL.format(i), original)

    return text, count, changes


# load_cta 의 정의는 core/text_metrics 로 옮겼다.
#
# CTA 를 '붙이는 쪽'(여기)과 '분량에서 빼는 쪽'(text_metrics)이 반드시
# 같은 파일을 봐야 하는데, 정의가 여기 있으면 core 가 agents 를
# 임포트하게 되어 계층이 뒤집힌다. 기존 호출부 호환을 위해 재수출한다.


def postprocess(
    text: str,
    append_cta: bool = True,
    cta_path: Path | None = None,
) -> PostProcessResult:
    """초안을 발행 형식으로 정규화한다.

    순서가 중요하다. 마크다운을 먼저 걷어내야 `## 첫 번째는`처럼
    기호가 붙은 챕터도 번호를 받는다.
    """
    changes: list[str] = []

    text, md_count, md_changes = strip_markdown_residue(text)
    changes += md_changes

    text, ch_count, ch_changes = number_chapters(text)
    changes += ch_changes

    text, st_count, st_changes = fix_style(text)
    changes += st_changes

    text = text.rstrip()

    cta_appended = False
    if append_cta:
        cta = load_cta(cta_path)
        if cta:
            text = f"{text}\n\n\n{cta}"
            cta_appended = True
            changes.append("CTA 문구 추가")

    return PostProcessResult(
        text=text,
        chapters_numbered=ch_count,
        style_fixes=st_count,
        markdown_removed=md_count,
        cta_appended=cta_appended,
        changes=changes,
    )
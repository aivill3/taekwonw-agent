"""발행 분량 측정 — 이 프로젝트에서 '몇 자'의 정의는 여기 하나뿐이다.

왜 별도 모듈인가
---------------
분량을 재는 곳이 셋이다. 작성기(절단) · 검사기(경고) · 알림(표시).
셋이 각자 세면 기준이 어긋난다. 실제로 그랬다.

  실측(2026-09-08, 같은 글 한 편):
    Slack  2,664자   len(markdown) — 줄바꿈·마커·CTA 전부 포함
    형태소 2,284자   마크다운·CTA 제거, 줄바꿈은 포함
    구조   2,248자   줄바꿈 제거, CTA 는 포함
  세 숫자가 다 다르고, 프롬프트가 지시한 1,900자는 그중 어느 것도 아니었다.

core/text_normalizer.py 에 넣지 않은 이유
----------------------------------------
그쪽은 '같은 말인가'를 판정하기 위한 매칭용 정규화 도구다(금칙어 검사).
여기는 '몇 자인가'의 정의다. 섞어 두면 strip_markdown 을 금칙어 쪽 사정으로
고쳤을 때 발행 분량이 조용히 따라 움직인다.

무엇을 빼는가
------------
LLM 이 쓴 산문만 센다. 다음은 빼는데, 이유가 각각 다르다.

  줄바꿈·빈 줄  발행물의 분량이 아니다. 프롬프트도 그렇게 지시한다
                ("줄바꿈과 빈 줄은 이 글자 수에 포함하지 않습니다")
  블록 마커     `/* 소제목 */` 등은 조판 지시다. 4챕터 글이면 150자를
                넘어 무시할 수 없다
  제로폭 공백   seo_checker._effective_lines 가 빼므로 여기서도 뺀다.
                한쪽만 빼면 두 숫자가 몇 자씩 어긋나고, 그 몇 자 때문에
                '기준이 하나'라는 보장이 깨진다
  CTA           LLM 이 쓰지 않는다. 후처리가 붙이는 고정 문구다.
                이걸 빼야 '붙기 전(작성기)'과 '붙은 뒤(검사기)'가 같은
                값을 낸다. 안 빼면 양쪽에 175자 보정이 필요해지고,
                분량을 아는 파일이 그만큼 늘어난다

무엇을 남기는가
--------------
제목 줄과 이미지 자리 숫자는 남긴다. 합쳐 30자 안쪽이라 판정을 바꾸지
않으면서, 빼려면 원고 구조를 파싱해야 해서 이득보다 비용이 크다.

성립하는 등식
------------
    prose_chars(markdown) == QualityChecker(...).check(...).seo.body_chars

전자는 clean_for_metrics() 뒤에 빈 줄을 빼고 길이를 합하고, 후자는
같은 텍스트에 seo_checker._effective_lines 를 적용해 같은 연산을 한다.
두 구현이 우연히 맞는 상태이지 코드가 강제하지는 않으므로, 어느 한쪽을
고칠 때는 반드시 대조할 것.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from config.settings import GUIDE_DIR

# 제로폭 공백. seo_checker._ZWSP 와 같은 값이다.
_ZWSP = "\u200b"

# 발행용 블록 마커. 사람이 서식을 적용할 위치 표시일 뿐 글의 일부가 아니다.
# 남겨두면 Kiwi 가 '본문'을 실제 단어로 세어(실측: 주요 키워드 7~8위)
# 형태소 총량·어휘 다양성·키워드 밀도가 모두 왜곡된다.
_RE_BLOCK_MARKER = re.compile(r"/\*\s*(?:소제목|본문|인용구)\s*\*/")


@lru_cache(maxsize=None)
def load_cta(path: Path | None = None) -> str:
    """발행 시 붙일 고정 문구를 읽는다. 파일이 없으면 빈 문자열.

    CTA 를 LLM 이 쓰게 두면 매 글마다 문구가 미묘하게 달라져 브랜드
    일관성이 깨진다. 그래서 파일로 고정하고 후처리가 붙인다.

    draft_postprocessor 에 있던 것을 옮겨 왔다. 붙이는 쪽과 세는 쪽이
    같은 파일을 봐야 하는데, 세는 쪽(core)이 붙이는 쪽(agents)을
    임포트하면 계층이 뒤집힌다.
    """
    p = path or (GUIDE_DIR / "cta.txt")
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def strip_cta(text: str) -> str:
    """원고 끝에 붙은 CTA 블록을 떼어낸다. 안 붙어 있으면 그대로 돌려준다.

    끝에서만 찾는다. 본문 중간에 우연히 같은 문구가 있어도 건드리지
    않는다 — 그건 LLM 이 CTA 를 흉내 낸 것이므로 분량에 세는 것이 맞고,
    프롬프트가 금지한 행동이라 오히려 드러나야 한다.
    """
    cta = load_cta()
    if not cta:
        return text
    stripped = text.rstrip()
    if not stripped.endswith(cta):
        return text
    return stripped[: -len(cta)].rstrip()


def clean_for_metrics(text: str) -> str:
    """분량·구조 측정 대상 텍스트를 만든다. 줄 구조는 그대로 둔다.

    이 프로젝트에서 '측정 대상'의 정의는 이 함수 하나다.
    숫자가 필요한 쪽(prose_chars)과 텍스트가 필요한 쪽(quality_gate)이
    갈라지더라도, 전처리는 반드시 여기를 거친다.

    줄을 합치거나 빈 줄을 지우지 않는 이유: seo_checker 가 줄 단위로
    20자 이하 비율·최장 줄을 재고, 문단 경계를 빈 줄로 찾는다.
    여기서 줄 구조를 뭉개면 그 지표들이 전부 무너진다.
    """
    body = strip_cta(text)
    return "\n".join(
        _RE_BLOCK_MARKER.sub("", line).replace(_ZWSP, "").rstrip()
        for line in body.split("\n")
    )


def prose_chars(text: str) -> int:
    """발행 분량의 단일 기준. CTA 부착 전후로 같은 값이 나온다.

    프롬프트가 지시하는 '본문 1,700~1,900자'가 이 단위다.
    작성기의 절단 기준, 검사기의 상한, Slack 표시가 모두 이것을 쓴다.
    """
    return sum(
        len(line.strip())
        for line in clean_for_metrics(text).split("\n")
        if line.strip()
    )
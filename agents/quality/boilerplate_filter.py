"""보일러플레이트 라인 제외 필터.

문제
----
파이프라인이 자동으로 붙이는 정형 문구가 분석 대상에 섞인다.

    사진: Martin.que / Pexels · 이미지는 이해를 돕기 위한 자료사진입니다
      -> '돕다:+2'로 잡혀 긍정 문장 1건이 늘어난다.
      -> '사진, 이미지, 이해, 자료'가 명사 통계에 포함돼 키워드 밀도가 흐려진다.

이 문구는 글쓴이가 쓴 본문이 아니라 템플릿이다. 매 글마다 동일하게 붙으므로
포함시키면 모든 글의 감정 비율이 같은 방향으로 밀린다.

해결
----
분석 전에 라인 단위로 걸러낸다. 제외된 라인은 QualityReport에 남겨
무엇이 빠졌는지 확인할 수 있게 한다.

패턴은 data/dictionaries/boilerplate_patterns.json 에서 읽으므로
파이프라인 템플릿이 바뀌면 코드 수정 없이 대응할 수 있다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from config.settings import DICT_DIR, GUIDE_DIR


@dataclass
class ExcludedLine:
    """제외된 라인 1건."""

    line_number: int  # 1-based
    text: str
    rule: str  # 어떤 규칙에 걸렸는지

    def to_dict(self) -> dict:
        return {
            "line_number": self.line_number,
            "text": self.text,
            "rule": self.rule,
        }


def _cta_patterns(cta_path: Path | None = None) -> list[re.Pattern]:
    """cta.txt 의 각 줄을 '줄 전체 일치' 패턴으로 만든다.

    본문 문장을 우연히 지우지 않도록 부분 일치가 아니라 전체 일치로 묶는다.
    CTA 는 고정 문구를 통째로 덧붙이는 것이라 줄이 그대로 나오기 때문이다.
    파일이 없으면 빈 목록 — CTA 를 안 쓰는 설정이므로 걸러낼 것도 없다.
    """
    path = cta_path or (GUIDE_DIR / "cta.txt")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []

    patterns: list[re.Pattern] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        patterns.append(re.compile(rf"^\s*{re.escape(s)}\s*$"))
    return patterns


class BoilerplateFilter:
    """캡션·출처·저작권 등 정형 문구를 본문에서 제거한다."""
    def __init__(self, rules: dict[str, list[re.Pattern]]) -> None:
        self._rules = rules

    @classmethod
    def load(
        cls,
        dict_dir: Path | None = None,
        filename: str = "boilerplate_patterns.json",
    ) -> "BoilerplateFilter":
        """패턴 사전을 읽어 필터를 만든다.

        파일이 없으면 예외를 던진다. 조용히 빈 규칙으로 넘어가면
        캡션·출처 문구가 그대로 감정 분석에 섞이는데도 아무 신호가 없어
        원인을 찾기 어렵다. 사전 누락은 즉시 드러나야 한다.

        필터를 의도적으로 끄려면 CheckConfig(exclude_boilerplate=False)를
        사용한다. 그 경우 이 메서드는 호출되지 않는다.
        """
        path = (dict_dir or DICT_DIR) / filename
        if not path.exists():
            raise FileNotFoundError(
                f"보일러플레이트 패턴 사전을 찾을 수 없습니다: {path}\n"
                f"파일이 없으면 캡션·출처 문구가 감정 분석에 섞입니다.\n"
                f"의도적으로 끄려면 CheckConfig(exclude_boilerplate=False)를 사용하세요."
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        rules: dict[str, list[re.Pattern]] = {}
        for key, conf in data.get("rules", {}).items():
            rules[key] = [re.compile(p) for p in conf.get("patterns", [])]

        # CTA 는 패턴 사전이 아니라 cta.txt 에서 직접 읽어 규칙을 만든다.
        #
        # 문구를 JSON 에 옮겨 적으면 두 파일이 어긋난다. cta.txt 를 고친 뒤
        # 사전을 안 고치면 CTA 가 본문으로 측정되는데, 지금 CTA 는 해요체와
        # 합니다체가 섞여 있어 문체 일관성 점수가 그대로 깎인다. 분량 검사도
        # 200자쯤 부풀려진다.
        rules["cta"] = rules.get("cta", []) + _cta_patterns()
        return cls(rules)

    def _match_rule(self, line: str) -> str | None:
        for key, patterns in self._rules.items():
            for pattern in patterns:
                if pattern.search(line):
                    return key
        return None

    def apply(self, text: str) -> tuple[str, list[ExcludedLine]]:
        """보일러플레이트 라인을 제거한 본문과 제외 목록을 반환한다.

        라인 수를 유지하지 않고 실제로 삭제한다. 빈 줄로 치환하면
        문장 분리기가 그 자리를 문장 경계로 오인할 수 있기 때문이다.
        """
        if not self._rules:
            return text, []

        kept: list[str] = []
        excluded: list[ExcludedLine] = []

        for i, line in enumerate(text.split("\n"), start=1):
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue

            rule = self._match_rule(stripped)
            if rule is None:
                kept.append(line)
            else:
                excluded.append(
                    ExcludedLine(line_number=i, text=stripped, rule=rule)
                )

        return "\n".join(kept), excluded

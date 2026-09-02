"""금칙어 검사.

세 가지 매칭 방식을 병행한다.

  1) surface : 공백·특수문자를 제거한 정규화 문자열에서 탐색한다.
               "최 고!!" 같은 우회 표기를 잡는다.
  2) lemma   : 형태소 원형으로 비교한다.
               '짜증나서', '짜증났다'를 모두 '짜증나다' 하나로 잡는다.
  3) pattern : 정규식. "100 % 효과", "업계 1위"처럼 숫자가 끼는 표현용.

용언(원형이 '다'로 끝나는 항목)은 lemma 매칭이, 명사·구절은 surface 매칭이
주력이다. 두 방식이 같은 위치를 잡으면 중복 제거한다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agents.quality.quality_models import BannedHit, Severity
from core.korean_morphology import SentenceUnit
from config.settings import DICT_DIR
from core.text_normalizer import make_context, normalize_with_map


@dataclass
class BannedCategory:
    """금칙어 카테고리 1개."""

    key: str
    label: str
    severity: Severity
    words: list[str]
    patterns: list[re.Pattern]
    description: str = ""


class BannedWordChecker:
    """카테고리별 금칙어 사전을 로드해 본문을 검사한다."""

    def __init__(self, categories: list[BannedCategory]) -> None:
        self.categories = categories
        # 원형이 '다'로 끝나면 용언으로 보고 lemma 매칭 대상에 넣는다.
        self._lemma_index: dict[str, BannedCategory] = {}
        self._surface_index: dict[str, tuple[BannedCategory, str]] = {}
        for cat in categories:
            for word in cat.words:
                if word.endswith("다"):
                    self._lemma_index[word] = cat
                normalized, _ = normalize_with_map(word)
                if normalized:
                    self._surface_index[normalized] = (cat, word)

    @classmethod
    def load(
        cls,
        dict_dir: Path | None = None,
        filename: str = "banned_words.json",
    ) -> "BannedWordChecker":
        path = (dict_dir or DICT_DIR) / filename
        if not path.exists():
            raise FileNotFoundError(f"금칙어 사전을 찾을 수 없습니다: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        categories: list[BannedCategory] = []
        for key, conf in data.get("categories", {}).items():
            severity = (
                Severity.BLOCK
                if str(conf.get("severity", "block")).lower() == "block"
                else Severity.WARN
            )
            categories.append(
                BannedCategory(
                    key=key,
                    label=conf.get("label", key),
                    severity=severity,
                    words=list(conf.get("words", [])),
                    patterns=[re.compile(p) for p in conf.get("patterns", [])],
                    description=conf.get("description", ""),
                )
            )
        return cls(categories)

    # --- 내부 매칭 ---------------------------------------------------------

    def _match_surface(self, text: str) -> list[BannedHit]:
        normalized, index_map = normalize_with_map(text)
        hits: list[BannedHit] = []
        for norm_word, (cat, original) in self._surface_index.items():
            start = normalized.find(norm_word)
            while start != -1:
                end_idx = start + len(norm_word) - 1
                orig_start = index_map[start]
                orig_end = index_map[end_idx] + 1
                hits.append(
                    BannedHit(
                        word=original,
                        matched=text[orig_start:orig_end],
                        category=cat.key,
                        category_label=cat.label,
                        severity=cat.severity,
                        start=orig_start,
                        end=orig_end,
                        context=make_context(text, orig_start, orig_end),
                        match_type="surface",
                    )
                )
                start = normalized.find(norm_word, start + 1)
        return hits

    def _match_lemma(self, text: str, sentences: list[SentenceUnit]) -> list[BannedHit]:
        hits: list[BannedHit] = []
        for sent in sentences:
            for unit in sent.units:
                cat = self._lemma_index.get(unit.lemma)
                if cat is None:
                    continue
                # Kiwi 토큰의 start/len은 문장 기준이 아니라 원문 절대 오프셋이다.
                orig_start = unit.start
                orig_end = unit.end
                hits.append(
                    BannedHit(
                        word=unit.lemma,
                        matched=text[orig_start:orig_end],
                        category=cat.key,
                        category_label=cat.label,
                        severity=cat.severity,
                        start=orig_start,
                        end=orig_end,
                        context=make_context(text, orig_start, orig_end),
                        match_type="lemma",
                    )
                )
        return hits

    def _match_pattern(self, text: str) -> list[BannedHit]:
        hits: list[BannedHit] = []
        for cat in self.categories:
            for pattern in cat.patterns:
                for m in pattern.finditer(text):
                    hits.append(
                        BannedHit(
                            word=pattern.pattern,
                            matched=m.group(0),
                            category=cat.key,
                            category_label=cat.label,
                            severity=cat.severity,
                            start=m.start(),
                            end=m.end(),
                            context=make_context(text, m.start(), m.end()),
                            match_type="pattern",
                        )
                    )
        return hits

    @staticmethod
    def _dedupe(hits: list[BannedHit]) -> list[BannedHit]:
        """같은 위치를 여러 방식이 잡은 경우 하나만 남긴다."""
        seen: set[tuple[int, int, str]] = set()
        result: list[BannedHit] = []
        for hit in sorted(hits, key=lambda h: (h.start, -(h.end - h.start))):
            key = (hit.start, hit.end, hit.category)
            if key in seen:
                continue
            seen.add(key)
            result.append(hit)
        return result

    # --- 공개 API ----------------------------------------------------------

    def check(self, text: str, sentences: list[SentenceUnit]) -> list[BannedHit]:
        """본문에서 금칙어를 찾아 적발 목록을 반환한다."""
        hits = (
            self._match_surface(text)
            + self._match_lemma(text, sentences)
            + self._match_pattern(text)
        )
        return self._dedupe(hits)

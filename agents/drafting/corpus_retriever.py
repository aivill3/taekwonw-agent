"""과거 블로그 글 검색기 (RAG의 R).

목적
----
새 글을 쓸 때 **비슷한 주제의 과거 글 2~3편**을 찾아 프롬프트에 예시로 넣는다.
LLM이 우리 블로그의 문체·구성·분량을 흉내 내게 하기 위한 것이다.

무엇을 검색 대상으로 삼는가
--------------------------
- 과거 블로그 글 (`rag/corpus/*.md`) -> 검색 O. 수십~수백 편이고 일부만 관련 있다
- 작성 가이드 (`rag/guide/*.md`)     -> 검색 X. 항상 전문 주입

가이드를 쪼개서 검색하면 "제목 길이" 청크만 걸리고 "금칙어" 청크는 빠지는
식으로 규칙이 누락된다. 규칙서는 부분이 아니라 전체가 의미를 갖는다.

검색 방식
--------
기본은 형태소 기반 BM25다. 임베딩 모델을 받지 않아도 되고, 결과가
결정론적이며, 태권도 고유명사(국기원, 겨루기, 품새) 매칭에 강하다.
코퍼스가 수백 편을 넘어가면 임베딩 검색으로 교체를 검토한다.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from core.korean_morphology import get_kiwi
from config.settings import CORPUS_DIR
from core.text_normalizer import strip_markdown

INDEX_PATH = CORPUS_DIR / ".bm25_index.json"

# 검색어로 의미 있는 품사만 사용한다. 조사·어미는 변별력이 없다.
_CONTENT_TAGS = {"NNG", "NNP", "VV", "VA", "SL", "SN"}

# BM25 파라미터. 일반적인 기본값.
_K1 = 1.5
_B = 0.75


@dataclass
class Document:
    """코퍼스 문서 1편."""

    doc_id: str
    path: str
    title: str
    text: str
    tokens: list[str]

    @property
    def length(self) -> int:
        return len(self.tokens)


@dataclass
class SearchHit:
    """검색 결과 1건."""

    doc_id: str
    title: str
    score: float
    text: str
    path: str


def tokenize(text: str) -> list[str]:
    """검색용 토큰 추출. 내용어 원형만 남긴다."""
    kiwi = get_kiwi()
    tokens = []
    for token in kiwi.tokenize(text):
        tag = token.tag.split("-", 1)[0]
        if tag not in _CONTENT_TAGS:
            continue
        form = token.form
        if tag in {"VV", "VA"}:
            form += "다"
        if len(form) < 2 and tag not in {"SL", "SN"}:
            continue
        tokens.append(form)
    return tokens


class BlogCorpus:
    """과거 블로그 글 코퍼스와 BM25 검색.

        corpus = BlogCorpus.load()
        hits = corpus.search("안산컵 국제오픈 태권도대회", top_k=3)
    """

    def __init__(self, documents: list[Document]) -> None:
        self.documents = documents
        self._df: Counter[str] = Counter()
        for doc in documents:
            self._df.update(set(doc.tokens))
        self._avg_len = (
            sum(d.length for d in documents) / len(documents) if documents else 0.0
        )

    def __len__(self) -> int:
        return len(self.documents)

    # --- 적재 -------------------------------------------------------------

    @classmethod
    def load(
        cls,
        corpus_dir: Path | None = None,
        use_cache: bool = True,
    ) -> "BlogCorpus":
        """코퍼스 디렉터리의 .md 파일을 모두 읽는다.

        토큰화는 파일 수에 비례해 느려지므로 결과를 캐시한다.
        캐시는 파일 목록과 수정 시각이 바뀌면 자동으로 무효화된다.
        """
        base = corpus_dir or CORPUS_DIR
        base.mkdir(parents=True, exist_ok=True)
        paths = sorted(
            p for p in base.iterdir() if p.suffix.lower() in (".md", ".txt")
        )

        signature = [[p.name, p.stat().st_mtime_ns] for p in paths]
        cache_path = base / ".bm25_index.json"

        if use_cache and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("signature") == signature:
                    docs = [
                        Document(
                            doc_id=d["doc_id"],
                            path=d["path"],
                            title=d["title"],
                            text=d["text"],
                            tokens=d["tokens"],
                        )
                        for d in cached["documents"]
                    ]
                    return cls(docs)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass  # 캐시가 깨졌으면 무시하고 다시 만든다

        documents: list[Document] = []
        for path in paths:
            raw = path.read_text(encoding="utf-8")
            plain = strip_markdown(raw)
            if not plain.strip():
                continue
            documents.append(
                Document(
                    doc_id=path.stem,
                    path=str(path),
                    title=_extract_title(raw) or path.stem,
                    text=plain,
                    tokens=tokenize(plain),
                )
            )

        if use_cache:
            cache_path.write_text(
                json.dumps(
                    {
                        "signature": signature,
                        "documents": [
                            {
                                "doc_id": d.doc_id,
                                "path": d.path,
                                "title": d.title,
                                "text": d.text,
                                "tokens": d.tokens,
                            }
                            for d in documents
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        return cls(documents)

    # --- 검색 -------------------------------------------------------------

    def _idf(self, term: str) -> float:
        n = len(self.documents)
        df = self._df.get(term, 0)
        # BM25 표준 IDF. 음수가 되지 않도록 +1 처리.
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 3) -> list[SearchHit]:
        """질의와 가장 유사한 과거 글을 반환한다."""
        if not self.documents:
            return []

        q_tokens = tokenize(query)
        if not q_tokens:
            return []

        results: list[SearchHit] = []
        for doc in self.documents:
            tf = Counter(doc.tokens)
            score = 0.0
            for term in set(q_tokens):
                freq = tf.get(term, 0)
                if freq == 0:
                    continue
                norm = 1 - _B + _B * (doc.length / self._avg_len if self._avg_len else 1)
                score += self._idf(term) * (freq * (_K1 + 1)) / (freq + _K1 * norm)
            if score > 0:
                results.append(
                    SearchHit(
                        doc_id=doc.doc_id,
                        title=doc.title,
                        score=round(score, 4),
                        text=doc.text,
                        path=doc.path,
                    )
                )

        results.sort(key=lambda h: h.score, reverse=True)
        return results[:top_k]


_H1 = re.compile(r"^\s{0,3}#\s+(.+)$", re.MULTILINE)


def _extract_title(markdown: str) -> str | None:
    """H1이 있으면 그것을, 없으면 첫 내용 줄을 제목으로 본다.

    네이버 블로그 글은 마크다운 헤딩을 쓰지 않으므로 첫 줄이 제목이다.
    """
    m = _H1.search(markdown)
    if m:
        return m.group(1).strip()
    for line in markdown.split("\n"):
        stripped = line.replace("\u200b", "").strip()
        if stripped:
            return stripped
    return None
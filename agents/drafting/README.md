# drafting — 초안 작성 (LLM #2)

**이 폴더에는 서로 다른 설계의 초안 생성 계통이 둘 있다.** 통합 전 상태이므로
어느 쪽을 고치는지 먼저 확인해야 한다.

| | `schema_draft_agent.py` | `draft_prompt.py` 계열 |
|---|---|---|
| 출력 | JSON 스키마 → 코드가 마크다운 조립 | LLM이 자유 텍스트 직접 출력 |
| 형식 | `# `, `## ` 마크다운 | `1. ` 챕터, 20자 줄바꿈 |
| 과거 글 참조 | 없음 | BM25 RAG |
| 가이드 | 프롬프트에 포함 | `prompts/draft/writing_guide.md` 전문 주입 |
| 분량 | 원문 × 1.8배, 상한 2,000자 | 4단계 티어 |
| 자동 파이프라인 | **사용 중** | 미사용 (수동 CLI만) |

`workflows/publish_workflow.py` 는 `schema_draft_agent` 만 호출한다.

## 왜 둘인가

`schema_draft_agent` 가 먼저 있었고, 실측으로 확인한 태권월드 실제 문체(숫자 챕터,
20자 줄바꿈, 마크다운 거의 0)를 반영하려고 자유 텍스트 계열을 새로 만들었다.
그런데 두 방식은 부분 교체가 안 된다.

`RESPONSE_SCHEMA` 가 `title`/`intro`/`sections`/`outro` 구조를 API 레벨에서
강제하고, `to_markdown()` 이 그걸 `## ` 로 조립한다. 네이버 형식으로 가려면
스키마가 통째로 무의미해진다. 그런데 스키마는 구조 안정성과 파싱 신뢰성을
담당한다 — `section_headings()` 가 `## ` 로 삽화 단위를 뽑을 수 있는 이유가
코드가 직접 붙였기 때문이다.

## 통합 판단 근거

`data/quality/metrics.jsonl` 에 매 실행마다 두 값이 쌓인다.

- **현행 형식 점수** — 마크다운 기준 구조 검사
- **네이버 전환 거리** — 같은 텍스트를 네이버 기준으로 재검사

핵심 지표는 `naver_gap.short_line_ratio`(20자 이하 줄 비율)다. 이 값이

- 70% 이상 → 후처리로 줄바꿈만 넣으면 해결. 스키마 유지 가능
- 30%대 → 문장 구조부터 다시 써야 함. 자유 텍스트로 전환이 맞음

샘플 측정에서는 33%가 나왔다. 실제 초안 여러 편으로 확인이 필요하다.

## 파일

| 파일 | 역할 |
|---|---|
| `schema_draft_agent.py` | JSON 스키마 방식 생성 + 마크다운 조립 + 길이 절단 |
| `draft_prompt.py` | 가이드 + 과거 글 + 소재를 프롬프트로 조립, `SeoConfig` 반환 |
| `corpus_retriever.py` | 과거 글 BM25 검색 (RAG의 R) |
| `article_grouper.py` | 기사를 주제형·날짜형으로 자동 묶음 |
| `draft_postprocessor.py` | LLM이 반복해서 못 지키는 형식 규칙을 코드로 교정 |

## `build_prompt()` 가 `SeoConfig` 를 함께 돌려주는 이유

목표 분량과 챕터 수는 소재 정보량에 따라 초안마다 달라진다. 프롬프트에 지시한
숫자를 그대로 검사 임계값으로 넘겨야 "1,300자로 쓰세요"라고 시킨 뒤 "1,600자
미만입니다"라고 경고하는 모순이 생기지 않는다.

```python
prompt, hits, seo_cfg = build_prompt(brief, corpus)
draft = llm(prompt)
report = checker.check(draft, keyword, seo_config=seo_cfg)
```

## 원문 없이 생성하지 말 것

소주제만으로 쓰면 LLM이 기사에 없는 수치·인물·날짜를 채워 넣는다. 원문을 근거로
제시하고 "원문에 있는 사실만 사용"을 명시해야 한다. 구글 RSS 경로는 본문 추출이
막히므로(`tools/README.md` 참고) 원문 없이 이 단계에 도달하지 않도록 상위에서
걸러야 한다.

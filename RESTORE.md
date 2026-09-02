# 복원 안내 (2026-08-19)

원본 프로젝트가 유실되어, 2026-08-06 시점 백업(file.7z)과 대화 중 개별
업로드된 파일을 합치고 **빠진 부분을 다시 만들어** 채운 것입니다.

**먼저 원본 복구를 시도해 보세요.** 하나라도 되면 그쪽이 훨씬 낫습니다.

```powershell
cd C:\dev\taekwonw-agent-v0.3
git reflog                 # 커밋 이력이 있으면 여기서 되살아납니다
git fsck --lost-found      # 커밋 안 한 blob 이 남아 있을 수 있습니다
```

휴지통 / OneDrive·Dropbox 버전 기록 / `%LOCALAPPDATA%\Temp` /
`.venv` 안의 `__pycache__` (.pyc 는 디컴파일 가능) 도 확인 대상입니다.
`data/drafts/` 의 과거 원고와 `logs/` 도 회수하면 좋습니다.

---

## 신뢰 수준을 나눠서 보세요

같은 "복원"이라도 근거가 다릅니다. 이 구분이 중요합니다.

### A등급 — 원본 그대로

백업과 업로드에 실물이 있던 파일입니다. 손대지 않았습니다.

`config/` 전체 · `agents/` 의 collecting·ranking·drafting·illustration·quality·subtopic ·
`workflows/` 4개 · `core/logger.py` · `tools/` 의 article_fetcher, google_rss_client,
notion_store, slack_notifier · 각 패키지 README

이 대화에서 함께 고친 내용도 최신 상태로 반영돼 있습니다 (아래 "반영된 수정" 참고).

### B등급 — 사용처에서 역산 (11개 파일)

실물은 없지만, **호출하는 쪽 코드가 남아 있어 인터페이스가 확정적**입니다.
필드 이름·함수 시그니처·반환 형식은 맞지만, 내부 구현 방식은 원본과 다를 수 있습니다.

| 파일 | 확정 근거 |
|---|---|
| `run.py` | 각 워크플로의 `run()` 시그니처 |
| `core/article_models.py` | 전 파일의 `a.*` 속성 접근, `save_csv` 의 `to_dict()` 사용 |
| `core/korean_morphology.py` | `LemmaUnit.lemma/tag/start/end`, `SentenceUnit.index/text/units` |
| `core/korean_morphology_types.py` | `quality_gate` 가 접근하는 필드 전부 |
| `core/prompt_loader.py` | `load_prompt("subtopic/batch.md")` 호출 형태 |
| `core/text_normalizer.py` | `banned_rules` 의 `index_map[start]` 인덱스 지도 계약 |
| `core/state_store.py` | `st["last_collected_at"]`, `mark_processed(st, urls, until)` |
| `tools/gemini_client.py` | 세 에이전트의 import 목록과 호출 인자 |
| `tools/naver_news_client.py` | 로그의 `30건 수집 (raw: naver_*.json)` 문구까지 일치 |
| `tools/diffusers_client.py` | 로그 문구 + settings 주석이 명시한 `_guidance()` |
| `tools/model_state.py` | 이 대화에서 새로 만든 것 (원래 없던 파일) |

### C등급 — 새로 작성 (12개 파일)

**원본과 다릅니다.** 형식(플레이스홀더·JSON 스키마)은 코드에서 확정했지만,
**내용은 제가 쓴 것**입니다. 실제 발행 글을 보시며 손보셔야 합니다.

| 파일 | 형식 확정 근거 | 내용 |
|---|---|---|
| `prompts/subtopic/single.md` | `{count} {title} {body}` + 응답 스키마 | 추정 |
| `prompts/subtopic/batch.md` | `{n} {articles}` + 배치 스키마 | 추정 |
| `prompts/image/illustration_concept.md` | `{headings}` + CONCEPT_SCHEMA | 추정 |
| `prompts/draft/writing_guide.md` | — | **전부 추정. 가장 중요하고 가장 불확실** |
| `prompts/draft/cta.txt` | — | 자리표시자 |
| `knowledge/dictionaries/*.json` (7개) | 각 로더의 파싱 형식 | 추정 |

`writing_guide.md` 가 글 품질을 좌우합니다. 출력 형식(마커·이미지 자리·챕터 수)은
`draft_prompt.py` 가 직접 지시하므로, 이 파일에는 문체 규칙만 담았습니다.

`SentiWord_info.json` (KNU 한국어 감성사전) 은 공개 배포본이라 다시 받을 수 있습니다.
없으면 `sentiment_fallback.json` 이 대신 쓰이지만 표제어가 훨씬 적습니다.
→ https://github.com/park1200656/KnuSentiLex

`knowledge/corpus/` 는 **비어 있습니다.** 과거 발행 글을 넣어야 문체 예시(RAG)가
동작합니다. 없어도 파이프라인은 돌아갑니다(경고만 남습니다).

---

## 검증한 것

```
구문 검사            79개 파일 오류 0건
워크플로 import      collect / subtopic / publish / images 모두 OK
CLI                  run.py --help 정상
사전 로더            금칙어 6카테고리 · 증감동사 45개 · 보일러플레이트 3줄 제거 확인
품질 검사 실행       금칙어 3건 · 문체 혼용 · 챕터 부족 정상 검출
                     형태소/감정/SEO 분석 동작
```

**API 를 실제로 호출하는 경로는 검증하지 못했습니다** (키가 없으므로).
네이버 수집, Gemini 호출, Notion 읽기/쓰기는 첫 실행에서 확인해야 합니다.

---

## 시작하기

```powershell
# 1. 압축 풀기 → 프로젝트 폴더로
# 2. 가상환경
py -m venv .venv
.venv\Scripts\activate

# 3. 의존성 (torch 는 용량이 크니 IMAGE_SOURCE=gemini 로 쓸 거면 빼도 됩니다)
pip install -r requirements.txt

# 4. 자격증명
copy .env.example .env
notepad .env        # 키 4종을 채웁니다

# 5. 형태소 분석기 첫 실행 (사전 다운로드, 1~2분)
py -c "from core.korean_morphology import get_kiwi; get_kiwi()"

# 6. 검사 경로부터 확인 (API 를 쓰지 않습니다)
py run.py check --file prompts/draft/writing_guide.md

# 7. 드라이런 (Notion 에 쓰지 않고 LLM 도 부르지 않습니다)
py run.py collect --dry-run

# 8. 실제 실행
py run.py collect
```

**첫 커밋을 바로 하세요.** 같은 일이 반복되지 않도록.

```powershell
git init
git add .
git commit -m "복원 시점"
```

`.gitignore` 에 `.env` 와 `data/`, `logs/` 를 넣어 뒀습니다.

---

## 실행 순서

```
py run.py collect                수집 → 정제 → Notion → 랭킹 → 소주제
  [Notion 에서 '승인' 체크]      ← 사람
py run.py publish                초안 작성 → Notion 저장 → 삽화
```

삽화를 나중에 붙이려면:

```
py run.py publish --no-images    초안만 (2~3분)
py run.py images                 시간 될 때 삽화만
```

`subtopic` 은 순서상 사이가 아니라 **보조 경로**입니다. 랭킹이 놓친 기사를
Notion 에서 '선정요청' 으로 바꿔 뒀을 때만 씁니다.

---

## 반영된 수정 (8/6 백업 이후)

이 대화에서 함께 고친 내용입니다.

- 당일 뉴스 기준 선정 + 후보 부족 시 시간창 자동 확대
- 표 레이아웃 기사가 통째로 지워지던 문제 (`remove_caption_lines`)
- 섹션 머리글이 본문 맨 위에 올 때 전체 삭제되던 문제
- 정제 단계별 진단 로그 `diagnose()`
- 정제 '전' CSV 백업 (제외된 기사도 보존)
- 구글 RSS 원본 JSON 저장 (네이버와 규약 통일)
- 타임아웃·503 재시도, 폴백 모델 전환
- 모델 일일 한도 소진 기록 (`tools/model_state.py`)
- Notion 저장을 삽화보다 먼저 (중단돼도 초안 보존)
- Notion API 연결 오류 재시도
- 로그 파일명에 단계 접두사 (`collect_*.log`)
- `images` 기본 대상을 당일 초안으로 제한

---

## 남은 일

1. `prompts/draft/writing_guide.md` 를 실제 블로그 톤에 맞게 다시 씁니다
2. `knowledge/corpus/` 에 과거 발행 글을 채웁니다
3. KNU 감성사전을 내려받아 `SentiWord_info.json` 으로 둡니다
4. `knowledge/dictionaries/` 의 금칙어·중립어를 실제 운영 기준으로 손봅니다
5. `.venv` 가 남아 있다면 `pip freeze` 로 원래 버전을 회수합니다

# quality — 블로그 초안 품질 검사

AI가 작성한 초안을 **형태소 분석 → 감정 비율 · 금칙어 · 문체 통계**로 검증하는 결정론적 게이트입니다.
LLM 호출이 전혀 없으므로 같은 입력이면 항상 같은 출력이 나옵니다.

## 설치

이 프로젝트에는 `pyproject.toml`이 없고 `requirements.txt` + `.venv`로 관리되므로
`uv add`가 아니라 `uv pip install`을 쓴다. (`uv add`는 `pyproject.toml`을 요구한다)

```powershell
cd C:\dev\taekwonw-agent-v0.2      # 반드시 프로젝트 루트에서
uv pip install kiwipiepy
Add-Content requirements.txt "`nkiwipiepy>=0.17.0"
```

## 파이프라인에서의 위치

```
초안 생성(LLM) ─┬─ 팩트체크 (코드)
                └─ 품질검사 (본 모듈, 코드) ─→ Notion 리포트 ─→ 사람 최종검토 ─→ 발행
```

## 파일 구성

| 파일 | 역할 |
|---|---|
| `quality_agent.py` | 오케스트레이터 + 리포트 포매터 |
| `seo_checker.py` | 제목·본문 길이, 챕터, 줄바꿈, 헤딩·불릿 구조 |
| `sentiment_analyzer.py` | 감성사전 로더, 문장 극성 판정 |
| `context_rules.py` | 논항 기반 문맥 극성 규칙 (증감 동사) |
| `banned_rules.py` | 금칙어 3중 매칭 (surface / lemma / pattern) |
| `boilerplate_filter.py` | 캡션·출처·저작권 라인 제외 필터 |
| `quality_models.py` | 결과 dataclass. 전부 `to_dict()` 지원 |
| `quality_gate.py` | 파이프라인 측정 어댑터 (발행을 막지 않음) |

형태소 분석기는 이 폴더에 없다. 초안 검색기(`agents/drafting/corpus_retriever`)도
같은 Kiwi 토크나이저를 쓰기 때문에 `core/korean_morphology.py` 로 내렸다.
임계값은 `config/quality_config.py` 에 있다.

## 데이터 파일

| 경로 | 설명 |
|---|---|
| `knowledge/dictionaries/banned_words.json` | 카테고리별 금칙어. 운영하며 계속 추가 |
| `knowledge/dictionaries/domain_neutral.json` | 태권도 용어 감정 중립화 예외 |
| `knowledge/dictionaries/domain_sentiment.json` | KNU 미등재 도메인 감정어 보강 |
| `knowledge/dictionaries/direction_verbs.json` | 증감 방향 동사 (증가/감소) |
| `knowledge/dictionaries/valence_nouns.json` | 논항 명사 극성 사전 |
| `knowledge/dictionaries/boilerplate_patterns.json` | 분석 제외 라인 패턴 |
| `knowledge/dictionaries/sentiment_fallback.json` | 내장 최소 감성사전 |
| `knowledge/dictionaries/SentiWord_info.json` | **KNU 감성사전 (직접 다운로드 필요)** |

KNU 사전 설치 (**프로젝트 루트에서 실행**):

```powershell
mkdir -Force data\dictionaries
curl.exe -sL -o data\dictionaries\SentiWord_info.json `
  https://raw.githubusercontent.com/park1200656/KnuSentiLex/master/data/SentiWord_info.json
```

경로에 `data/`가 들어간다는 점에 주의. 레포 루트가 아니라 `data/` 하위에 있다.
`curl`이 아니라 `curl.exe`를 쓰는 이유는 PowerShell의 `curl`이
`Invoke-WebRequest` 별칭이라 인자 해석이 다르기 때문이다.

정상 다운로드 시 약 1.1MB, 표제어 14,854개다.

파일이 있으면 자동으로 우선 사용하고, 없으면 fallback으로 동작합니다.

## 판정 규칙

| 항목 | 결과 |
|---|---|
| `severity: block` 금칙어 1건 이상 | `passed = False` (발행 차단) |
| `severity: warn` 금칙어 | 리포트에만 표시, 발행 허용 |
| 감정 비율 / 키워드 밀도 / 문체 / 길이 | `warnings`에 문구 추가, 발행 허용 |

감정 비율을 차단 근거로 쓰지 않는 이유는 감성사전의 도메인 편차 때문입니다.
사람이 최종 검토할 때 참고하는 숫자로만 취급합니다.

## 금칙어 3중 매칭

| 방식 | 대상 | 예시 |
|---|---|---|
| `lemma` | `다`로 끝나는 용언 | `찌질하다` → 찌질했어요 / 찌질해서 모두 적발 |
| `surface` | 명사·구절, 우회 표기 | `최고` → "최  고!!" 적발 |
| `pattern` | 숫자·조사가 끼는 표현 | `100 % 보장`, `업계 1위` |

Kiwi가 원형 복원에 실패하는 신조어(`빡치다` → `빡`+`치다`)는 정규식으로 보완합니다.

## 도메인 중립화가 필요한 이유

범용 감성사전은 **격파·공격·제압·타격·패배·혹독하다**를 전부 부정어로 잡습니다.
그대로 두면 정상적인 경기 리포트가 "부정 40%"로 나와 지표를 신뢰할 수 없습니다.
`domain_neutral.json`이 이 단어들을 사전에서 제거해 0점으로 만듭니다.

리포트를 며칠 돌려보며 오탐 단어를 계속 추가하세요.

## 문맥 의존 극성 규칙

`늘어나다`처럼 그 자체로 극성이 없는 증감 동사는 사전 점수를 무시하고
**논항 명사의 극성 x 동사의 방향 부호**로 계산한다.

```
참가자가 늘어나다  ->  참가자(+1) x 증가(+1) = +1  긍정
피해가   늘어나다  ->  피해(-1)   x 증가(+1) = -1  부정
부상자가 줄어들다  ->  부상자(-1) x 감소(-1) = +1  긍정
```

논항 명사의 극성은 3단계로 조회한다.

| 순서 | 출처 | 예 |
|---|---|---|
| 1 | `valence_nouns.json` 도메인 사전 | 참가자, 수련생, 반칙, 부상자 |
| 2 | 감성사전(KNU)의 명사 점수 | 먼지(-1) |
| 3 | 없으면 **0 (중립)** | 비중 -> 극성미상 |

3단계가 핵심이다. 모르는 명사에 부호를 찍는 대신 중립으로 둔다.
사전 방식의 오탐은 대부분 확신 없이 부호를 찍어서 생긴다.

## 감정 사전 3계층

조회 순서가 중요하다. 뒤쪽이 앞쪽을 덮어쓴다.

| 계층 | 파일 | 역할 |
|---|---|---|
| 1 | `SentiWord_info.json` | KNU 범용 감성사전 14,656개 |
| 2 | `domain_sentiment.json` | KNU 미등재 도메인 감정어 보강 |
| 3 | `domain_neutral.json` | 최종 중립화. 조회 자체를 막는다 |

2계층이 필요한 이유는 KNU에 `논란·문제·우려·오심`이나
`우승·기량·성과` 같은 단어가 없기 때문이다. 이것들이 빠지면
스포츠 기사에서 긍정도 부정도 거의 잡히지 않는다.

3계층은 `격파·제압·타격`처럼 기술을 서술할 뿐 가치 판단이 없는 용어에만
적용한다. **`부상·실격`처럼 스포츠 문맥에서도 실제로 나쁜 소식인 단어는
중립화하지 말 것.** 초기 버전에서 이 둘을 훈련 관련어와 함께 묶었다가
부정 기사가 전혀 탐지되지 않는 문제가 있었다.

## 감정 지표 두 가지

| 지표 | 계산식 | 성격 |
|---|---|---|
| `positive_ratio` 외 | 각 분류 ÷ 전체 문장 | 3분류. 세 값의 합이 1 |
| `binary_positive_ratio` | 긍정 ÷ (긍정 + 부정) | 이진. 중립 제외 |

이진 지표는 일부 블로그 분석 도구가 쓰는 척도라 **수치 비교용 참고값**으로만
제공한다. 감정 문장이 적으면 크게 흔들리므로 목표치로 삼지 말 것.
감정 문장이 하나도 없으면 `None`을 반환한다. 0.0으로 두면 '전부 부정'과
구분되지 않기 때문이다.

발행 차단은 어느 쪽 지표로도 하지 않는다. 감성사전의 도메인 편차가 커서
판정 근거로 쓰기엔 신뢰도가 부족하다.

## 보일러플레이트 제외

파이프라인이 자동으로 붙이는 정형 문구는 글쓴이가 쓴 본문이 아니므로
형태소 분석 **전에** 라인 단위로 걸러낸다.

```
사진: Martin.que / Pexels · 이미지는 이해를 돕기 위한 자료사진입니다
  -> '돕다:+2'로 잡혀 긍정 문장이 1건 늘어난다
  -> '사진, 이미지, 이해, 자료'가 명사 통계에 포함된다
```

매 글마다 동일하게 붙는 문구라서, 포함시키면 모든 글의 감정 비율이
같은 방향으로 밀린다.

| 규칙 | 대상 |
|---|---|
| `image_credit` | `사진:`, `출처:`, Pexels/Unsplash 등 |
| `image_disclaimer` | "이해를 돕기 위한", "본문과 관련 없는" |
| `copyright` | 무단전재 금지, ⓒ, All rights reserved |
| `meta_notice` | `※`로 시작, 해시태그 나열, 구독 유도 |
| `source_link` | `원문:`, 단독 URL 라인 |

제외된 라인은 `report.excluded_lines`에 규칙명과 함께 남으므로
무엇이 빠졌는지 확인할 수 있다. 필터를 끄려면
`CheckConfig(exclude_boilerplate=False)`.

템플릿이 바뀌면 `boilerplate_patterns.json`에 패턴만 추가하면 된다.

## 사용법

```python
from src.quality import QualityChecker, format_report

checker = QualityChecker()          # Kiwi·사전 로딩. 인스턴스 재사용 권장
report = checker.check(draft_markdown)

if not report.passed:
    for hit in report.blocking_hits:
        print(hit.matched, hit.category_label, hit.context)

print(format_report(report))                    # Slack·Notion용 요약
json.dumps(report.to_dict(), ensure_ascii=False) # 구조화 저장용
```

CLI:

```powershell
python run.py check --file data/drafts/2026-07-31.md
python run.py check --file draft.md --json --out data/reports/quality.json
```

차단 시 exit code 1을 반환하므로 GitHub Actions 스텝이 자동으로 실패합니다.

## 임계값 조정

```python
from src.quality import CheckConfig, QualityChecker

config = CheckConfig(
    max_keyword_density=0.08,
    min_style_consistency=0.90,
    min_char_count=1200,
)
checker = QualityChecker(config=config)
```

## 테스트

```powershell
uv run pytest tests/test_quality.py -v
```
## 구조·형식 검사 (`seo_checker.py`)

형태소·감정·금칙어 검사가 **평문**을 대상으로 하는 것과 달리, 이 모듈만
**마크다운 원문**을 본다. `strip_markdown()` 을 거치면 헤딩과 줄바꿈이 사라져
구조를 셀 수 없기 때문이다.

| | `NAVER_BLOG` (기본) | `MARKDOWN` |
|---|---|---|
| 챕터 | `1. 첫 번째는 ~` 숫자 | `## 소제목` |
| 헤딩·불릿 | **금지** — 경고 대상 | 정상 |
| 줄 길이 | 20자 이하 90% 이상 | 검사 안 함 |
| 이미지 | 검사 안 함 | `![]()` 개수·대체텍스트 |

기본값이 `NAVER_BLOG` 인 근거는 태권월드 기존 발행 글 2편 실측이다. 마크다운
헤딩 0개, 불릿 0개, 줄 길이 평균 12자(98%가 20자 이하), 숫자 챕터 4개였다.

### 임계값

`config/quality_config.py` 의 `SeoConfig` 에 있다.

| 항목 | 기본값 | 근거 |
|---|---|---|
| `title_min` / `title_max` | 15 / 40 | 실측 20~22자. 업계 통설 '30~40자'는 네이버 공식 근거가 없어 하한으로 삼지 않음 |
| `body_min_chars` / `body_max_chars` | 1600 / 2800 | 실측 순수 본문 1,825~2,102자 |
| `min_chapters` | 3 | |
| `max_line_chars` | 20 | |
| `min_short_line_ratio` | 0.90 | |

분량과 챕터 수는 소재 정보량에 따라 초안마다 달라진다. `build_prompt()` 가
`SeoConfig` 를 만들어 넘기면 그 값이 우선하고, 위 기본값은 주입이 없을 때의
폴백이다.

```python
prompt, hits, seo_cfg = build_prompt(brief, corpus)
report = checker.check(draft, keyword, seo_config=seo_cfg)
```

### 구조 경고는 발행을 막지 않는다

구조 미달은 법적 위험이 아니라 품질 문제이고, 짧은 속보처럼 규칙을 벗어나는 것이
맞는 글도 있다. 발행 차단은 오직 `severity=block` 금칙어에서만 발생한다.

구조 경고는 `report.seo.warnings` 와 `report.warnings`(`[구조]` 접두사) 양쪽에
들어간다. 파이프라인이 `report.warnings` 만 보고 분기해도 놓치지 않게 하기 위함이다.

### ⚠️ 글자 수 단위가 두 가지다

| 값 | 계산 | 포함 |
|---|---|---|
| `QualityReport.char_count` | `len(body)` | 줄바꿈 포함, 보일러플레이트 제외 |
| `SeoReport.body_chars` | 유효 줄 길이 합 | 줄바꿈 제외, 보일러플레이트 포함 |

네이버 형식은 20자마다 줄을 끊어 두 값의 격차가 20%를 넘기도 한다. 마크다운
형식에서는 3% 남짓이다. 프롬프트가 지시하는 분량 기준은 `body_chars` 쪽이다.

## ⚠️ 임계값의 성격

여기 숫자들은 **"네이버가 요구하는 기준"이 아니다.** 업계에 도는 SEO 규칙은
네이버 공식 문서에 근거가 없다. 이 값들은 태권월드 기존 발행 글을 실측해서 나온,
**기존 글과 스타일을 일치시키기 위한** 기준이다. 이 구분이 흐려지면 나중에
임계값을 조정할 때 판단 근거가 사라진다.

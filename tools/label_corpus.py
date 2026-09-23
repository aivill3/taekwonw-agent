#!/usr/bin/env python
"""라벨 코퍼스 구축 — 수집 CSV 에 '이 기사는 무엇에 관한 글인가' 를 붙인다.

    py tools/label_corpus.py --stats                현황 · LLM/사람 일치율
    py tools/label_corpus.py --llm --naver-only     네이버 수집분만 일괄 라벨링
    py tools/label_corpus.py --review 30            LLM 라벨 30건을 사람이 검수
    py tools/label_corpus.py --manual --limit 50    손으로 라벨링
    py tools/label_corpus.py --llm --since 2026-09-09
    py tools/label_corpus.py --file data/processed/collected_20260922_101114.csv

--naver-only 를 권하는 이유
-------------------------
2026-09 시점 코퍼스 1,009건 중 871건이 구글 RSS 시절 수집분이다. 제목에
' - 매체명' 이 붙고 search_keyword 가 비어 있다. 지금 파이프라인은 네이버
API 만 쓰므로, 그 기사들을 섞어 임계값을 맞추면 실제 실행에 안 맞는 값이
나온다. 채점 셋은 지금 파이프라인이 실제로 보는 모집단이어야 한다.

왜 필요한가
----------
지금 주제 적합성 임계값(MIN_CORE_HITS, MIN_CORE_DENSITY, MIN_CORE_MENTIONS)은
수집분을 눈으로 훑어 정한 값이다. 그래서 "바꿨더니 나아졌나" 를 물으면
답할 방법이 없다. 2026-09-22 실행에서 두 실패가 같은 날 함께 나왔다.

    통과   이동섭 전 의원 "게임·e스포츠…제2국기원 통해"   ← 주제가 아닌데 개수는 채움
    탈락   '품새 퀸' 이주영, 세계선수권 4연패            ← 주제인데 본문이 짧음

손잡이 하나로 두 방향을 동시에 고칠 수 없다. 어느 쪽으로 얼마나 틀렸는지
숫자로 봐야 하고, 그러려면 정답이 있어야 한다. 이 파일이 그 정답을 만든다.

라벨 4종 — 발행 카테고리와 같다
-----------------------------
    조직   국기원·세계태권도연맹·대한태권도협회 등 태권도 행정 조직이 주어인
           기사. 운영·인사·정책·국제교류·승단/단증/심사·이사회·징계
    대회   태권도 경기가 주제인 기사. 대회 결과·성적·선수 활약·대회 개최
    태권도 그 밖에 태권도가 주제인 기사. 도장·수련·시범·승급 심사·인물·
           문화·산업·지역 태권도 소식
    무관   통과시키면 안 되는 기사 (아래 참고)

'무관' 의 뜻
-----------
"태권도가 안 나온다" 가 아니다. 이 코퍼스는 전부 태권도 검색 결과라
태권도가 한 번도 안 나오는 기사는 없다. 기준은 하나다.

    이 기사 하나로 블로그 글의 한 챕터(400~500자)를 쓸 수 있는가?

못 쓰는 이유는 셋이다 — 내용이 없거나(캡션·단신), 태권도 얘기가 한 줄뿐이거나
(종합 브리핑·다른 분야 기사), 실으면 안 되는 소재(사건·사고·가십)거나.

사건 기사를 '무관' 에 넣는 이유: 게이트(is_on_topic)가 EXCLUDE_CRIME 으로
그 기사를 떨어뜨린다. 정답도 '떨어져야 함' 이어야 채점 신호가 맞는다.
'대회' 로 두면 제대로 일한 필터가 누락(FN)으로 감점된다.

'대회' 와 '태권도' 를 가르는 이유
------------------------------
2026-09-22 까지는 3분류였고 '대회' 가 태권도 활동 전반을 뜻했다. 그래서
라벨 146건 중 101건(69%)이 '대회' 로 몰려, 발행 카테고리로 쓸 수 없었다.
카테고리를 세 개로 늘리면서 '대회' 를 경기 기사로 좁히고 '태권도' 를
포괄 카테고리로 뒀다.

⚠️ 3분류 시절 라벨은 '대회' 의 뜻이 달라 그대로 쓸 수 없다.
   --relabel 로 다시 돌려야 한다.

파이프라인은 건드리지 않는다
--------------------------
저장된 CSV 만 읽고 data/quality/labels.csv 만 쓴다. Notion·상태·초안에
손대지 않는다. LLM 은 라벨 '생성기' 로만 쓴다 — 런타임 판정자가 아니다.
(LLM 호출은 소주제·초안 두 곳뿐이라는 원칙은 그대로다)

labels.csv 가 본문을 통째로 품는 이유
----------------------------------
채점할 때 collected_*.csv 와 조인하면, 오래된 CSV 가 정리된 뒤에는 라벨만
남고 본문이 사라진다. 그때부터 과거 라벨로 다시 채점할 수 없다. 자족적으로
둔다 — 200건 × 2,000자면 400KB 남짓이라 감당할 만하다.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from agents.collecting.body_cleaner import clean_all              # noqa: E402
from config.draft_config import (                                 # noqa: E402
    SUBTOPIC_FALLBACK_MODELS,
    SUBTOPIC_MODEL,
)
from config.settings import (                                     # noqa: E402
    GEMINI_API_KEY,
    KST,
    PROCESSED_DIR,
    QUALITY_DIR,
    REQUEST_TIMEOUT,
)
from core.article_models import Article, canonical_url            # noqa: E402
from tools.gemini_client import (                                 # noqa: E402
    ModelExhausted,
    api_url,
    is_daily_quota,
    looks_like_zero_quota,
    violated_quota_ids,
    wait_seconds,
)

LABELS_PATH = QUALITY_DIR / "labels.csv"

# 라벨 이름 = 레인 이름 = Notion '검색키워드' 값. 셋을 같은 문자열로 두면
# 판정 결과가 곧 발행 카테고리가 되어, 중간 매핑 표가 필요 없다.
ORG = "조직"
EVENT = "대회"
GENERAL = "태권도"
OFF = "무관"
LABELS = (ORG, EVENT, GENERAL, OFF)

# 손으로 라벨링할 때 누르는 키. 한글 입력기를 켜지 않아도 되게 숫자로 둔다.
# 순서는 LANES 의 레인 순서와 같다 (뉴스가 적은 쪽부터).
KEYMAP = {"1": ORG, "2": EVENT, "3": GENERAL, "4": OFF}

# labels.csv 열. 순서를 바꾸면 기존 파일과 어긋나므로 뒤에만 덧붙인다.
FIELDS = [
    "url", "title", "press", "published", "search_keyword", "search_rank",
    "chars", "body_clean",
    "label", "labeled_by", "labeled_at",
    "llm_label", "llm_why",
]

# 한 번에 LLM 에 보낼 기사 수. 8건 × 본문 1,200자면 프롬프트가 1만 자 안쪽이라
# flash-lite 로도 안정적이다. 더 키우면 뒤쪽 기사의 판정이 거칠어진다.
LLM_BATCH_SIZE = 8
LLM_BODY_LIMIT = 1200
MAX_RETRIES = 3

# 이번 실행에서 한도가 찬 모델. 배치 18회에 매번 죽은 모델부터 다시 부르면
# 429 를 그만큼 더 받는다.
#
# agents.illustration.model_chain 을 쓰지 않는 이유: 그 모듈은 소진 기록을
# 전역에 두고 삽화 경로 전체가 공유한다. 오프라인 라벨링 도구가 파이프라인
# 모듈의 상태를 오염시킬 이유가 없다. 여기서는 이 파일 안에서만 기억한다.
_exhausted: set[str] = set()

SYSTEM_RULE = """당신은 태권도 전문 블로그의 기사 선별 담당자입니다.
독자는 수련생, 학부모, 도장 운영자입니다.
기사를 읽고 '무엇에 관한 글인가'로 분류하세요. 단어가 몇 번 나왔는지가
아니라, 기사의 주어와 주제가 무엇인지로 판단합니다.

분류
  조직 — 태권도 행정 조직이 주어인 기사.
         국기원, 세계태권도연맹(WT), 대한태권도협회, 국제태권도연맹(ITF),
         태권도진흥재단 등. 운영·인사·정책·예산·국제교류·승단/단증/심사·
         이사회/총회가 주제일 때.
  대회 — 태권도 경기가 주제인 기사.
         대회 결과, 메달·성적, 선수 활약, 대회 개최·폐막, 선수 선발.
         대회를 주최한 것이 협회여도, 기사의 주제가 경기면 '대회'입니다.
  태권도 — 위 둘에 해당하지 않으면서 태권도가 주제인 기사.
         도장·수련·시범단, 승급 심사, 인물 인터뷰, 태권도 문화·역사,
         품새·겨루기 소개, 지역 태권도 소식, 어린이·생활체육 태권도 교실,
         대회 기간에 열린 부대행사(걷기·전시·체험부스·문화공연 등).
  무관 — 통과시키면 안 되는 기사.

'무관'의 기준은 하나입니다.
  이 기사 하나로 블로그 글의 한 챕터(400~500자)를 쓸 수 있는가?
쓸 수 없으면 무관입니다. 못 쓰는 경우는 다섯입니다.
  · 내용이 없다 — 사진 캡션, 한 줄 단신, 부음, 동정
  · 태권도가 한 줄만 스쳐 나온다 — 다른 종목/분야 기사, e스포츠, 로봇,
    지자체·대학의 종합 브리핑, 행사 나열 중 한 꼭지가 태권도
  · 주어가 태권도가 아니다 — 정치인·기관장의 일정 기사에 태권도 행사
    참석이 포함된 것 (예: '○○시의회 의장, 태권도인들과 동행')
  · 실을 수 없는 소재 — 범죄·수사·재판·학대·가십. 태권도장 기사여도
    사건 보도면 무관입니다.
  · 협회 분쟁 — 징계, 항소심·소송·가처분, 제명, 직무정지, 사퇴 촉구,
    집단행동, 법정공방이 주제인 기사. 태권도 조직 기사여도 무관입니다.
    (예: '경북태권도협회, 경북체육회장 사퇴 촉구')
    조직의 운영·정책 기사와 헷갈리기 쉽습니다. 다툼이 주제면 무관입니다.

헷갈리기 쉬운 경계
  · 대회 소식이면서 조직 얘기도 있다 → 기사 제목이 가리키는 쪽
  · 어린이 태권도 교실, 생활체육 프로그램 → 태권도 (학부모 독자에게 소재가 됨)
  · 대회 기간의 부대행사 → 태권도. 경기가 아니다.
    (예: '세계 태권도인과 시민 500명, 평화 염원하며 발걸음' → 태권도)
    경기 결과·성적을 다루면 그때 대회입니다.
  · 정치인이 대회에 참석했다 → 기사의 주어가 대회면 대회, 정치인이면 무관
  · 선수 개인 인터뷰 → 경기 성적이 중심이면 대회, 삶·훈련 이야기면 태권도

출력은 JSON 배열만. 설명·코드펜스·머리말을 붙이지 마세요.
  [{"i": 1, "label": "조직", "why": "열 자 이내 근거"}, ...]
입력으로 받은 모든 번호에 대해 한 개씩 내야 합니다."""


# ── 입출력 ────────────────────────────────────────────────
def _load_csv(path: Path) -> list[Article]:
    """수집 CSV 한 장 → Article 목록. sweep.py / check_ranking.py 와 같은 방식."""
    fields = {f.name for f in Article.__dataclass_fields__.values()}
    out: list[Article] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            data = {k: v for k, v in row.items() if k in fields}
            data["search_rank"] = int(data.get("search_rank") or 0)
            data["subtopics"] = [
                s for s in (data.get("subtopics") or "").split("\n") if s.strip()
            ]
            for drop in (
                "keyword_score", "score_norm", "report_count", "matched_keywords"
            ):
                data.pop(drop, None)
            out.append(Article(**data))
    return out


def gather(files: list[Path]) -> dict[str, Article]:
    """CSV 여러 장을 읽어 URL 당 한 건으로 합친다. 먼저 온 쪽을 남긴다.

    collected_* 를 쓰는 이유: 이 파일에만 '탈락한 기사' 가 들어 있다.
    selected_* 는 이미 통과한 것만 담고 있어, 그것으로 채점하면 누락(FN)을
    영원히 측정할 수 없다.

    정제(clean_all)를 거치는 이유: collected_* 는 정제 '전' 백업이라
    body_clean 이 비어 있다. is_on_topic 은 body_clean 만 보므로,
    정제하지 않으면 전건이 '본문 0자' 로 탈락한다.
    """
    merged: dict[str, Article] = {}
    for path in files:
        try:
            got = _load_csv(path)
        except Exception as e:
            print(f"  건너뜀 {path.name}: {e}", file=sys.stderr)
            continue
        cleaned = clean_all(got)
        for a in cleaned:
            merged.setdefault(canonical_url(a.url), a)
        print(f"  {path.name}: {len(got)}건 → 정제 {len(cleaned)}건")
    return merged


def load_labels() -> dict[str, dict]:
    """기존 라벨. URL(정규화) → 행."""
    if not LABELS_PATH.exists():
        return {}
    rows: dict[str, dict] = {}
    with LABELS_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows[canonical_url(row.get("url", ""))] = row
    return rows


def save_labels(rows: dict[str, dict]) -> None:
    """전체를 다시 쓴다. 200건 규모라 부분 갱신보다 단순한 쪽이 낫다."""
    QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    with LABELS_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def _new_row(a: Article) -> dict:
    return {
        "url": a.url,
        "title": a.title,
        "press": a.press or a.source,
        "published": a.published,
        "search_keyword": a.search_keyword,
        "search_rank": a.search_rank,
        "chars": len(a.body_clean),
        "body_clean": a.body_clean,
        "label": "",
        "labeled_by": "",
        "labeled_at": "",
        "llm_label": "",
        "llm_why": "",
    }


def _stamp() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# ── 손 라벨링 ──────────────────────────────────────────────
def label_manually(targets: list[dict], rows: dict[str, dict], review: bool) -> None:
    """한 건씩 보여주고 키 입력을 받는다. 매 건 저장하므로 중단해도 안 잃는다."""
    total = len(targets)
    done = 0
    mode = "검수" if review else "라벨링"
    # 안내 문구를 손으로 적지 않는다. 라벨을 늘렸을 때 KEYMAP 만 고치고 문구를
    # 그대로 두면, 화면에 적힌 키와 실제 저장되는 라벨이 어긋난다.
    # (2026-09-22: 3분류 → 4분류로 늘린 뒤 '3=무관' 이 남아, 3을 누르면
    #  '태권도' 가 저장되는 상태로 검수가 시작될 뻔했다)
    keys = "  ".join(f"{k}={v}" for k, v in KEYMAP.items())
    print(
        f"\n{mode} 시작 — {total}건\n"
        f"  {keys}  Enter=건너뜀  q=저장하고 종료\n"
        + "─" * 70
    )
    for i, row in enumerate(targets, 1):
        body = (row.get("body_clean") or "").replace("\n", " ")
        print(f"\n[{i}/{total}] {row.get('title', '')}")
        print(
            f"  {row.get('press', '')} · {str(row.get('published', ''))[:10]}"
            f" · 검색 {row.get('search_keyword', '')} {row.get('search_rank', '')}위"
            f" · 본문 {row.get('chars', '')}자"
        )
        if review and row.get("llm_label"):
            print(f"  LLM 판정: {row['llm_label']} ({row.get('llm_why', '')})")
        print(f"  {body[:400]}{'…' if len(body) > 400 else ''}")

        try:
            key = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n중단합니다.")
            break
        if key == "q":
            break
        if key not in KEYMAP:
            continue

        row["label"] = KEYMAP[key]
        row["labeled_by"] = "review" if review else "manual"
        row["labeled_at"] = _stamp()
        rows[canonical_url(row["url"])] = row
        save_labels(rows)
        done += 1

    print(f"\n{mode} {done}건 저장 — {LABELS_PATH}")


# ── LLM 라벨링 ─────────────────────────────────────────────
def _build_payload(batch: list[dict]) -> dict:
    lines = []
    for i, row in enumerate(batch, 1):
        body = (row.get("body_clean") or "").replace("\n", " ")[:LLM_BODY_LIMIT]
        lines.append(f"[{i}] 제목: {row.get('title', '')}\n    본문: {body}")
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_RULE}]},
        "contents": [{"parts": [{"text": "\n\n".join(lines)}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }


def _parse(text: str, batch: list[dict]) -> dict[int, tuple[str, str]]:
    """응답 JSON → {1-based 인덱스: (라벨, 근거)}. 모르는 라벨은 버린다."""
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"  응답 파싱 실패: {e}", file=sys.stderr)
        return {}
    out: dict[int, tuple[str, str]] = {}
    for it in items if isinstance(items, list) else []:
        try:
            idx = int(it.get("i", 0))
            label = str(it.get("label", "")).strip()
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(batch) and label in LABELS:
            out[idx] = (label, str(it.get("why", ""))[:40])
    return out


def _call_batch(batch: list[dict], model: str) -> dict[int, tuple[str, str]] | None:
    """한 배치를 한 모델로 시도. 실패는 None, 한도 소진은 ModelExhausted.

    _try_models() 와의 계약이다 — None 이면 다음 모델로 넘어가고,
    ModelExhausted 면 그 모델을 이번 실행 내내 건너뛴다.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(
                api_url(model),
                headers={
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=_build_payload(batch),
                timeout=REQUEST_TIMEOUT * 12,
            )
            if resp.status_code == 429:
                try:
                    body_json = resp.json()
                except ValueError:
                    body_json = {}
                if looks_like_zero_quota(body_json) or is_daily_quota(body_json):
                    raise ModelExhausted(
                        f"한도 소진: {', '.join(violated_quota_ids(body_json))}"
                    )
                if attempt < MAX_RETRIES:
                    wait = wait_seconds(resp.text, attempt)
                    print(f"  RPM 초과, {wait:.0f}초 대기")
                    time.sleep(wait)
                    continue
                return None
            if resp.status_code == 404:
                raise ModelExhausted(f"모델 사용 불가(404): {model}")
            if not resp.ok:
                print(f"  오류 [{resp.status_code}]: {resp.text[:150]}", file=sys.stderr)
                return None
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            return _parse(text, batch)
        except ModelExhausted:
            raise
        except Exception as e:
            print(f"  요청 실패: {e}", file=sys.stderr)
            return None
    return None


def _try_models(batch: list[dict]) -> dict[int, tuple[str, str]] | None:
    """모델을 순서대로 시도한다. 한도 소진이면 다음 모델로 넘어간다.

    전부 실패하면 None — 호출부는 그 배치만 건너뛰고 계속 간다. 여기서
    예외를 던지면 앞서 라벨한 배치까지 날릴 이유가 없는데도 중단된다.
    """
    for model in [SUBTOPIC_MODEL, *SUBTOPIC_FALLBACK_MODELS]:
        if not model or model in _exhausted:
            continue
        try:
            got = _call_batch(batch, model)
        except ModelExhausted as e:
            print(f"  한도 소진, 다음 모델로: {model} ({e})")
            _exhausted.add(model)
            continue
        if got:
            return got
    return None


def label_with_llm(targets: list[dict], rows: dict[str, dict]) -> None:
    """미라벨분을 배치로 돌린다. 배치마다 저장해 중단에 대비한다."""
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY 가 없습니다. .env 를 확인하세요.", file=sys.stderr)
        return

    batches = [
        targets[i:i + LLM_BATCH_SIZE]
        for i in range(0, len(targets), LLM_BATCH_SIZE)
    ]
    print(
        f"\nLLM 라벨링 — {len(targets)}건 / {len(batches)}배치"
        f" (모델 {SUBTOPIC_MODEL})"
    )
    done = 0
    for n, batch in enumerate(batches, 1):
        result = _try_models(batch)
        if not result:
            print(f"  [{n}/{len(batches)}] 실패 — 건너뜁니다")
            continue
        for idx, (label, why) in result.items():
            row = batch[idx - 1]
            row["llm_label"] = label
            row["llm_why"] = why
            # 사람이 이미 본 건은 사람 라벨을 덮지 않는다.
            if row.get("labeled_by") not in ("manual", "review"):
                row["label"] = label
                row["labeled_by"] = "llm"
                row["labeled_at"] = _stamp()
            rows[canonical_url(row["url"])] = row
            done += 1
        save_labels(rows)
        print(f"  [{n}/{len(batches)}] {len(result)}건")

    print(f"\nLLM 라벨 {done}건 저장 — {LABELS_PATH}")
    print("⚠️ 30건 정도는 --review 로 검수해 일치율을 확인하세요. 일치율이 낮으면")
    print("   이 라벨을 정답으로 쓸 수 없습니다.")


# ── 현황 ──────────────────────────────────────────────────
def show_stats(rows: dict[str, dict]) -> None:
    if not rows:
        print(f"라벨이 없습니다 — {LABELS_PATH}")
        return

    labeled = [r for r in rows.values() if r.get("label")]
    print(f"\n■ 라벨 현황 — {LABELS_PATH.name}")
    print(f"  전체 {len(rows)}건 · 라벨 완료 {len(labeled)}건")

    by_label: dict[str, int] = {}
    by_who: dict[str, int] = {}
    for r in labeled:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
        by_who[r.get("labeled_by", "?")] = by_who.get(r.get("labeled_by", "?"), 0) + 1
    print("  분포: " + " · ".join(f"{k} {v}건" for k, v in sorted(by_label.items())))
    print("  출처: " + " · ".join(f"{k} {v}건" for k, v in sorted(by_who.items())))

    # 사람이 검수한 건만으로 LLM 일치율을 낸다. LLM 라벨을 정답으로 쓸지
    # 결정하는 유일한 근거다.
    checked = [
        r for r in labeled
        if r.get("labeled_by") == "review" and r.get("llm_label")
    ]
    if checked:
        agree = sum(1 for r in checked if r["label"] == r["llm_label"])
        print(f"\n■ LLM 일치율 — 검수 {len(checked)}건")
        print(f"  4분류 일치   {agree}/{len(checked)} ({agree / len(checked):.0%})")

        # 게이트(is_on_topic)는 통과/탈락만 판정한다. 조직·대회·태권도 사이의
        # 혼동은 eval_gate 의 점수를 전혀 바꾸지 않는다. 채점에 쓸 라벨로
        # 믿어도 되는지는 이 이진 일치율로 판단해야 한다.
        #
        # 4분류 일치율은 발행 카테고리 배정(= 나중에 만들 판정기)의 난이도를
        # 보여주는 별개 지표다. 두 숫자는 용도가 다르다.
        binary = sum(
            1 for r in checked if (r["label"] == OFF) == (r["llm_label"] == OFF)
        )
        print(
            f"  통과/탈락    {binary}/{len(checked)} ({binary / len(checked):.0%})"
            f"  ← 게이트 채점에 쓰이는 값"
        )

        wrong = [r for r in checked if r["label"] != r["llm_label"]]
        crossing = [
            r for r in wrong if (r["label"] == OFF) != (r["llm_label"] == OFF)
        ]
        if crossing:
            print(f"\n  통과/탈락이 갈린 {len(crossing)}건 — 이것만 채점에 영향")
            for r in crossing[:10]:
                print(f"    LLM {r['llm_label']} → 사람 {r['label']} | {r['title'][:42]}")
            if len(crossing) > 10:
                print(f"    … 외 {len(crossing) - 10}건")
        inside = [r for r in wrong if r not in crossing]
        if inside:
            print(f"\n  카테고리만 갈린 {len(inside)}건 — 채점에는 영향 없음")
            for r in inside[:6]:
                print(f"    LLM {r['llm_label']} → 사람 {r['label']} | {r['title'][:42]}")
            if len(inside) > 6:
                print(f"    … 외 {len(inside) - 6}건")
    else:
        print("\n  (검수분 없음 — --review 로 LLM 라벨을 확인하세요)")

    need = 150 - len(labeled)
    if need > 0:
        print(f"\n  채점에 쓰려면 {need}건 더 필요합니다 (권장 150~200건)")


# ── 진입점 ────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="수집 CSV 에 주제 라벨을 붙인다")
    p.add_argument("--manual", action="store_true", help="손으로 라벨링")
    p.add_argument("--llm", action="store_true", help="Gemini 로 일괄 라벨링")
    p.add_argument("--review", type=int, metavar="N", help="LLM 라벨 N건을 검수")
    p.add_argument("--stats", action="store_true", help="현황만 출력")
    p.add_argument("--file", action="append", help="대상 CSV (여러 번 지정 가능)")
    p.add_argument("--limit", type=int, default=0, help="이번에 처리할 최대 건수")
    p.add_argument("--relabel", action="store_true", help="이미 라벨된 것도 다시")
    p.add_argument(
        "--since", metavar="YYYY-MM-DD", help="이 날짜 이후 발행분만 라벨링"
    )
    p.add_argument(
        "--naver-only", action="store_true",
        help="검색 키워드가 있는 기사만 (= 네이버 수집분)",
    )
    args = p.parse_args()

    rows = load_labels()

    if args.stats or not (args.manual or args.llm or args.review):
        show_stats(rows)
        if not (args.manual or args.llm or args.review):
            print("\n무엇을 할지 고르세요: --manual · --llm · --review N")
        return

    # 검수는 기존 라벨만 본다. 새 CSV 를 읽을 필요가 없다.
    if args.review:
        targets = [
            r for r in rows.values()
            if r.get("llm_label") and r.get("labeled_by") == "llm"
        ]
        if not targets:
            print("검수할 LLM 라벨이 없습니다. 먼저 --llm 을 돌리세요.")
            return
        label_manually(targets[:args.review], rows, review=True)
        return

    # 라벨링은 수집 CSV 에서 후보를 채운다.
    files = (
        [Path(f) for f in args.file]
        if args.file
        else sorted(PROCESSED_DIR.glob("collected_*.csv"))
    )
    files = [f for f in files if f.exists()]
    if not files:
        print(f"수집 CSV 를 찾을 수 없습니다: {PROCESSED_DIR}", file=sys.stderr)
        raise SystemExit(1)

    print(f"수집 CSV {len(files)}장 읽는 중...")
    merged = gather(files)
    added = 0
    for key, a in merged.items():
        if key not in rows:
            rows[key] = _new_row(a)
            added += 1
    print(f"기사 {len(merged)}건 (신규 {added}건) · 기존 라벨 {len(rows) - added}건")

    targets = [
        r for r in rows.values()
        if args.relabel or not r.get("label")
    ]

    # 코퍼스에 구글 RSS 시절 기사가 섞여 있다 (2026-08-17~09-08, 871건).
    # 그 기사는 search_keyword 가 비어 있고 제목에 ' - 매체명' 이 붙는다.
    #
    # 섞어서 임계값을 맞추면 안 되는 이유: 수집 모집단도 본문 추출 성공률도
    # 지금 파이프라인(네이버 API)과 다르다. 그 위에서 최적화한 값은 실제
    # 실행에 맞지 않는다. 레인 배정 정확도는 search_keyword 가 없으면
    # 아예 잴 수 없다.
    #
    # 필터를 --limit 보다 앞에 두는 이유: 뒤에 두면 잘라낸 앞쪽 N건이
    # 전부 구버전일 때 라벨이 한 건도 안 붙는다.
    if args.naver_only:
        before = len(targets)
        targets = [r for r in targets if (r.get("search_keyword") or "").strip()]
        print(f"네이버 수집분만: {before}건 → {len(targets)}건")
    if args.since:
        before = len(targets)
        targets = [
            r for r in targets
            if str(r.get("published") or "")[:10] >= args.since
        ]
        print(f"{args.since} 이후: {before}건 → {len(targets)}건")

    if not targets:
        print("라벨할 기사가 없습니다. --relabel 로 다시 할 수 있습니다.")
        save_labels(rows)
        return
    if args.limit:
        targets = targets[:args.limit]

    if args.llm:
        label_with_llm(targets, rows)
    else:
        label_manually(targets, rows, review=False)

    show_stats(load_labels())


if __name__ == "__main__":
    main()
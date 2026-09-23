"""전역 설정: 환경변수, 경로, 상수를 한곳에서 관리한다."""
import os
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# 한국 표준시 (서머타임 없음 → 고정 오프셋으로 충분, 외부 패키지 불필요)
KST = timezone(timedelta(hours=9), name="KST")

# 프로젝트 루트 기준 절대경로 (실행 위치와 무관하게 동작)
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ── 데이터 경로 ──────────────────────────────────────────
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"            # 수집 직후 원본 (JSON/CSV)
PROCESSED_DIR = DATA_DIR / "processed"  # 본문 추출·정제 완료본
DRAFT_DIR = DATA_DIR / "drafts"
IMAGE_DIR = DATA_DIR / "images"
QUALITY_DIR = DATA_DIR / "quality"   # 품질 측정 누적 (metrics.jsonl)
for _d in (RAW_DIR, PROCESSED_DIR, DRAFT_DIR, IMAGE_DIR, QUALITY_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 실행 간 유지되는 상태 (버전 관리 대상) ──────────────
# data/ 는 필요한 것만 추적한다(.gitignore 에서 하위 항목을 개별 제외).
# GitHub Actions 는 실행마다 새 컨테이너라, 추적되지 않는 것은 매번
# 초기화된다. 그러면
#   - 스톡 재사용 간격(14일)이 무력화돼 같은 사진이 매일 나오고
#   - 모델 소진 기록이 사라져 한도가 끝난 모델에 매번 다시 들이받는다
# 그래서 커밋할 수 있는 자리로 뺀다. 워크플로가 실행 끝에 이 폴더만
# 커밋·푸시하면 다음 실행이 이어받는다.
#
# data/ 안에서 특정 파일만 되살리는 방법도 있지만(data/* + 부정 패턴),
# git 은 제외된 '디렉터리' 하위를 다시 포함하지 못해 패턴이 까다로워진다.
# 별도 폴더가 의도도 분명하다.
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── 지식 자료 경로 (LLM/검사기가 읽는 정적 자료) ────────
# data/ 는 런타임 산출물만, knowledge/ 는 버전 관리 대상 자료로 나눈다.
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
DICT_DIR = KNOWLEDGE_DIR / "dictionaries"   # 품질 검사 사전 (코드만 읽음)
CORPUS_DIR = KNOWLEDGE_DIR / "corpus"       # 과거 글 — BM25로 검색해 일부만 주입

# LLM 텍스트 모델. publish 경로에서는 초안을 쓴 모델이 넘어오지만
# `run.py images` 는 Notion 에서 초안을 읽어오므로 그 정보가 없다. 그때 쓸 기본값.
TEXT_MODEL = os.getenv("TEXT_MODEL", "gemini-flash-latest")

# 프롬프트는 코드처럼 관리한다. 파일로 두고 버전 이력을 남긴다.
PROMPT_DIR = BASE_DIR / "prompts"
GUIDE_DIR = PROMPT_DIR / "draft"            # 규칙서는 전문 주입

# ── API 자격증명 ─────────────────────────────────────────
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Content DB. 글 한 편(= 승인 1회)이 한 행이다.
# Notion 에서 빈 데이터베이스를 하나 만들고 그 ID를 넣으면
# 속성은 ensure_content_schema() 가 채운다.
NOTION_CONTENT_DATABASE_ID = os.getenv("NOTION_CONTENT_DATABASE_ID", "")

# 보드가 있는 Notion 페이지 ID. 그 페이지에 '📋 승인 불가 현황' 으로
# 시작하는 콜아웃 블록을 하나 만들어 두면, confirm 이 거부된 슬롯의
# 사유를 그 블록에 통째로 갈아 끼운다.
NOTION_BOARD_PAGE_ID = os.getenv("NOTION_BOARD_PAGE_ID", "")

# Slack Incoming Webhook (비어 있으면 알림을 건너뛴다)
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# 삽화 공급원:
#   "hybrid"  CLIP 스톡 검색 → 없으면 SDXL 생성 → 생성 실패면 스톡 대체 (기본)
#   "planned" 항상 생성
#   "local"   로컬 diffusers
#   "gemini"  API
#   "none"    삽화 없음
#
# 기본이 hybrid 인 이유: GPU 서버가 없거나 꺼져 있어도 글이 삽화와 함께
# 나온다. 생성이 안 되면 점수 하한 없이 스톡에서 최선을 채운다.
# (실측 2026-09-02: GPU 미연결 상태에서 4편 16자리가 전부 비었다)
#
# 초기 뉴스 사진을 그대로 쓰지 않는 이유는 저작권·초상권이다. 스톡은
# 라이선스가 명확한 것만 인덱싱해 쓴다.
#
# 주의: 스톡 장수가 부족하면 같은 사진이 돌아 나온다. 하루 4편 × 4자리에
# 재사용 간격 14일이면 200장 이상이 필요하다.
IMAGE_SOURCE = os.getenv("IMAGE_SOURCE", "hybrid")

# ── 로컬 이미지 생성 (diffusers) ──────────────────────
# CPU에서는 모델 선택이 곧 실행 시간이다. 기본값은 turbo 계열(1~4 step).
#   stabilityai/sd-turbo                      512px  CPU 30~90초/장
#   runwayml/stable-diffusion-v1-5            512px  CPU 2~5분/장
#   stabilityai/stable-diffusion-xl-base-1.0 1024px  CPU 5~10분/장
DIFFUSERS_MODEL = os.getenv("DIFFUSERS_MODEL", "stabilityai/sd-turbo")
DIFFUSERS_STEPS = int(os.getenv("DIFFUSERS_STEPS", "4"))
DIFFUSERS_WIDTH = int(os.getenv("DIFFUSERS_WIDTH", "768"))
DIFFUSERS_HEIGHT = int(os.getenv("DIFFUSERS_HEIGHT", "576"))
# turbo 계열에서는 0.0 이 강제된다 (diffusers_client._guidance)
DIFFUSERS_GUIDANCE = float(os.getenv("DIFFUSERS_GUIDANCE", "7.0"))


# ── Notion 보관 정책 ────────────────────────────────
# 정리 대상은 '상태' 기준.
#
# 원고 자체는 publish 가 로컬 파일로도 남기므로(data/drafts),
# Notion 쪽은 보관 기간을 짧게 잡아 페이지가 무한정 쌓이는 것을 막는다.
# 완전 삭제가 아니라 휴지통 이동이라 30일간 복구할 수 있다.
RETENTION_DAYS = {
    "수집됨": 1,        # 미선정 — 눈으로 훑어보는 기간
    "선정요청": 1,      # 사람이 골랐으나 소주제 생성 전
    "선정됨": 1,        # 소주제까지 만든 기사
    "초안요청": 1,      # 버튼이 눌렸으나 아직 작성 전
    "보류": 1,          # 검토 후 거절
}

# 작성완료도 정리하려면 .env 에 KEEP_WRITTEN=false, 그때 적용할 보관일수
KEEP_WRITTEN = os.getenv("KEEP_WRITTEN", "true").lower() != "false"
WRITTEN_RETENTION_DAYS = int(os.getenv("WRITTEN_RETENTION_DAYS", "30"))

# ── 네트워크 / 병렬 처리 ─────────────────────────────────
REQUEST_TIMEOUT = 10        # 초
MAX_WORKERS = 8             # 본문 추출 병렬 스레드 수
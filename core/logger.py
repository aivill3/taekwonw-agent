"""로깅 설정: 콘솔 + 실행별 파일(logs/{단계}_YYYYMMDD_HHMMSS.log) 이중 출력.

- 각 로그 줄에 'YYYY-MM-DD HH:MM:SS' 전체 타임스탬프 기록
- 타임스탬프는 실행 머신의 로컬 시간이 아닌 KST 고정
  (GitHub Actions 러너는 UTC라서, 고정하지 않으면 로컬 실행과 9시간 어긋남)

파일 이름의 단계 접두사:
  collect_20260814_094531.log / publish_20260814_091554.log 처럼 어느 명령의
  로그인지 이름만 보고 알 수 있다. 하루에 collect·publish·images 를 번갈아
  돌리면 pipeline_* 만으로는 열어봐야 구분이 된다.
  단계는 실행 인자에서 추론하므로 각 워크플로를 고칠 필요가 없다.

파일을 실행 단위로 나누는 이유:
  일자별로 누적하면 하루에 여러 번 돌렸을 때 어디부터가 이번 실행인지
  구분하려고 '파이프라인 시작' 줄을 찾아 거슬러 올라가야 한다. 실패한 실행과
  성공한 실행의 경고가 한 파일에 섞여 원인 판단을 흐린다.
  data/raw/naver_YYYYMMDD_HHMMSS.json 과 접미사가 같아 대조하기도 쉽다.

사용법 (각 모듈에서):
    from core.logger import get_logger
    log = get_logger(__name__)
    log.info(...)     # 진행 상황
    log.warning(...)  # 기사 단위 실패 (파이프라인은 계속)
    log.error(...)    # 파이프라인 중단급 오류
"""
import logging
import sys
from datetime import datetime
from pathlib import Path

from config.settings import BASE_DIR, KST

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# trafilatura는 파싱 불가한 HTML을 만나면 자신의 로거로 ERROR를 찍는다.
# 해당 기사는 article_fetcher가 조용히 버리므로 파이프라인에는 영향이 없는데,
# 루트 로거로 전파되어 로그 파일이 남의 ERROR로 뒤덮인다. WARNING 이상만 남긴다.
_NOISY_LOGGERS = {
    "trafilatura": logging.CRITICAL,
    # 모델 다운로드 시 파일마다 HTTP 요청 로그가 남아 로그 파일을 뒤덮는다.
    "httpx": logging.WARNING,
    "urllib3": logging.WARNING,
    "filelock": logging.WARNING,
    "diffusers": logging.WARNING,
    "transformers": logging.WARNING,
    "huggingface_hub": logging.ERROR,
}

_configured = False
_logfile: Path | None = None

# run.py 서브커맨드 목록. 로그 파일 이름의 접두사로 쓴다.
# argv 전체를 훑되 이 집합에 있는 것만 인정한다 — 위치로 잡으면
# 'py run.py --dry-run collect' 처럼 플래그가 앞에 올 때 '--dry-run' 을
# 단계 이름으로 오인한다.
_STAGES = frozenset({"collect", "subtopic", "publish", "images", "check"})
_DEFAULT_STAGE = "pipeline"


def _stage_from_argv() -> str:
    """실행 인자에서 단계 이름을 추론한다. 못 찾으면 'pipeline'."""
    for arg in sys.argv[1:]:
        if arg in _STAGES:
            return arg
    return _DEFAULT_STAGE


class KSTFormatter(logging.Formatter):
    """로그 타임스탬프를 머신 로컬 시간 대신 KST로 고정."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=KST)
        return dt.strftime(datefmt or _DATEFMT)


def setup(stage: str | None = None) -> Path:
    """루트 로거 설정. 파이프라인 시작 시 1회 호출. 로그 파일 경로를 반환한다.

    stage: 로그 파일 접두사(collect / publish / images / subtopic).
           생략하면 실행 인자에서 추론한다 → 워크플로를 고치지 않아도 동작한다.

    이미 설정됐으면 아무것도 하지 않고 기존 경로를 돌려준다. 한 프로세스에서
    워크플로를 두 번 부르더라도 로그가 두 파일로 쪼개지지 않게 하기 위함이다.
    """
    global _configured, _logfile
    if _configured and _logfile is not None:
        return _logfile

    stage = stage or _stage_from_argv()
    _logfile = LOG_DIR / f"{stage}_{datetime.now(KST):%Y%m%d_%H%M%S}.log"
    formatter = KSTFormatter(_FORMAT, datefmt=_DATEFMT)
    handlers = [logging.FileHandler(_logfile, encoding="utf-8"), logging.StreamHandler()]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)

    for name, level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

    _configured = True
    logging.getLogger(__name__).info(f"로그 파일: {_logfile.name}")
    return _logfile


def logfile() -> Path | None:
    """현재 실행의 로그 파일 경로. setup() 전이면 None."""
    return _logfile


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name.removeprefix("src."))
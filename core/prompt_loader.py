"""prompts/ 아래의 프롬프트 파일 로더.

프롬프트를 코드 문자열이 아니라 파일로 두는 이유:
  프롬프트는 코드보다 훨씬 자주 고친다. 파이썬 문자열 안에 있으면 따옴표와
  들여쓰기, 중괄호 이스케이프에 신경 써야 하고 diff 도 읽기 어렵다.
  파일로 두면 마크다운 그대로 편집하고 변경 이력도 깔끔하게 남는다.

모듈 로드 시점에 읽는다 (subtopic_agent 의 BATCH_PROMPT 등).
파일이 없으면 그 자리에서 실패해야 한다. 빈 문자열로 넘어가면 LLM 이
지시 없는 요청을 받아 엉뚱한 결과를 돌려주고, 원인은 한참 뒤에 드러난다.
"""
from functools import lru_cache

from config.settings import PROMPT_DIR
from core.logger import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=None)
def load_prompt(relative_path: str) -> str:
    """prompts/ 기준 상대 경로로 프롬프트를 읽는다.

    예) load_prompt("subtopic/batch.md") → prompts/subtopic/batch.md

    같은 파일을 여러 번 부르면 캐시에서 돌려준다. 배치 루프 안에서 호출해도
    디스크를 반복해 읽지 않는다.
    """
    path = PROMPT_DIR / relative_path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"프롬프트 파일이 없습니다: {path}\n"
            f"prompts/{relative_path} 를 만들어 주세요."
        ) from e
    except OSError as e:
        raise RuntimeError(f"프롬프트 읽기 실패 ({path}): {e}") from e

    if not text.strip():
        raise ValueError(f"프롬프트가 비어 있습니다: {path}")
    return text

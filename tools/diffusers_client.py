"""로컬 이미지 생성 (diffusers / Stable Diffusion).

무료 티어 이미지 API 의 일일 한도를 벗어나려고 둔 경로다. 대신 CPU 에서는
장당 수십 초~수 분이 걸린다.

파이프라인을 세우지 않는다:
  diffusers/torch 미설치, 모델 다운로드 실패, 메모리 부족 — 어느 쪽이든
  DiffusersUnavailable 로 알린다. 호출부(local_image_agent)는 이 예외를 받으면
  그때까지 만든 삽화만 들고 넘어간다. 삽화가 없어도 글은 글이다.

파이프라인을 전역 하나로 두는 이유:
  모델 로딩이 수 분 걸린다(최초 실행은 2.5GB 다운로드 포함). 이미지마다
  새로 만들면 글 한 편에 몇 시간이 든다. 한 번 올려 두고 재사용한다.
"""
import os
from pathlib import Path

from config.settings import (
    DIFFUSERS_GUIDANCE,
    DIFFUSERS_HEIGHT,
    DIFFUSERS_MODEL,
    DIFFUSERS_STEPS,
    DIFFUSERS_WIDTH,
)
from core.logger import get_logger

log = get_logger(__name__)

# 원격 GPU 서버 주소. 비어 있으면 이 컴퓨터의 GPU/CPU 로 직접 생성한다.
#   예) DIFFUSERS_ENDPOINT=http://localhost:8000  (SSH 터널)
#
# 공용 GPU 서버를 쓸 때를 위한 것이다. 프로젝트 전체를 서버에 올리면
# Notion·Gemini 자격증명까지 공용 장비에 남는다. 생성만 떼어 보내면
# 서버는 프롬프트와 그림만 다루고 키는 이쪽에 남는다.
DIFFUSERS_ENDPOINT = os.getenv("DIFFUSERS_ENDPOINT", "").rstrip("/")
GPU_SERVER_TOKEN = os.getenv("GPU_SERVER_TOKEN", "")

# 원격 생성 대기 상한(초). SDXL 30스텝이 4090에서 3~5초지만, 모델을 새로
# 올리는 첫 요청은 몇 분이 걸린다.
REMOTE_TIMEOUT = int(os.getenv("DIFFUSERS_REMOTE_TIMEOUT", "600"))

# turbo 계열은 1~4 스텝으로 그림이 나온다. 일반 SD 는 CPU 에서 20~50 스텝이
# 필요해 장당 10분을 넘긴다. 품질을 조금 내주고 시간을 택했다.
# 설정값은 config/settings.py 가 단일 출처다.

_pipe = None
_unavailable_reason = ""


class DiffusersUnavailable(RuntimeError):
    """로컬 이미지 생성을 쓸 수 없다. 호출부는 삽화 없이 진행해야 한다."""


def _guidance(model: str) -> float:
    """모델에 맞는 guidance_scale.

    turbo/lightning 계열은 CFG 없이 학습돼 있어 0.0 이 권장값이다.
    설정의 7.0 을 그대로 넣으면 이미지가 뭉개진다. 설정 하나로 모델을 바꿔가며
    쓸 수 있어야 하므로, 모델 이름을 보고 여기서 강제한다.
    """
    name = model.lower()
    if "turbo" in name or "lightning" in name or "lcm" in name:
        return 0.0
    return DIFFUSERS_GUIDANCE


def _device() -> str:
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _get_pipe():
    """파이프라인을 한 번만 만들어 재사용한다.

    한 번 실패하면 이유를 기억해 두고 이후 호출에서 바로 같은 예외를 던진다.
    모델 다운로드에 실패하는 상황이면 다시 시도해도 결과가 같은데, 그때마다
    수 분씩 기다리면 삽화 4장에 20분이 그냥 나간다.
    """
    global _pipe, _unavailable_reason
    if _unavailable_reason:
        raise DiffusersUnavailable(_unavailable_reason)
    if _pipe is not None:
        return _pipe

    try:
        import torch
        from diffusers import AutoPipelineForText2Image
    except ImportError as e:
        _unavailable_reason = (
            f"diffusers/torch 가 설치돼 있지 않습니다 ({e}). "
            f"`pip install diffusers torch` 또는 IMAGE_SOURCE=gemini 로 두세요."
        )
        raise DiffusersUnavailable(_unavailable_reason) from e

    device = _device()
    log.info(f"이미지 모델 로딩: {DIFFUSERS_MODEL} ({device}) — 최초 실행은 수 분 걸립니다")
    try:
        # CPU 에서 float16 은 지원되지 않거나 훨씬 느리다. 장치에 맞춰 고른다.
        dtype = torch.float16 if device == "cuda" else torch.float32
        pipe = AutoPipelineForText2Image.from_pretrained(DIFFUSERS_MODEL, torch_dtype=dtype)
        pipe = pipe.to(device)
        # NSFW 체커는 CPU 에서 생성 시간을 눈에 띄게 늘린다. 프롬프트가
        # 코드에서 조립되는 고정 문구라 외부 입력이 섞이지 않는다.
        if hasattr(pipe, "safety_checker"):
            pipe.safety_checker = None
        pipe.set_progress_bar_config(disable=True)
    except Exception as e:
        _unavailable_reason = f"이미지 모델 로딩 실패: {e}"
        raise DiffusersUnavailable(_unavailable_reason) from e

    _pipe = pipe
    return _pipe


def _generate_remote(
    prompt: str, negative: str, out_path: Path, w: int, h: int
) -> Path | None:
    """원격 GPU 서버에 생성을 맡긴다.

    서버가 죽어 있거나 응답이 없으면 DiffusersUnavailable 을 올린다.
    로컬로 조용히 넘어가지 않는다 — 원격을 쓰라고 설정해 뒀는데 CPU 로
    떨어지면 몇 시간이 지나서야 알아차리게 된다.
    """
    import requests

    headers = {}
    if GPU_SERVER_TOKEN:
        headers["Authorization"] = f"Bearer {GPU_SERVER_TOKEN}"

    try:
        resp = requests.post(
            f"{DIFFUSERS_ENDPOINT}/generate",
            json={
                "prompt": prompt,
                "negative": negative or "",
                "width": w,
                "height": h,
                "steps": DIFFUSERS_STEPS,
            },
            headers=headers,
            timeout=REMOTE_TIMEOUT,
        )
    except requests.exceptions.ConnectionError as e:
        raise DiffusersUnavailable(
            f"GPU 서버에 연결할 수 없습니다 ({DIFFUSERS_ENDPOINT}). "
            f"SSH 터널과 서버 실행 상태를 확인하세요: {e}"
        ) from e
    except requests.exceptions.Timeout as e:
        raise DiffusersUnavailable(
            f"GPU 서버 응답 시간 초과({REMOTE_TIMEOUT}초). "
            f"첫 요청은 모델 로딩으로 오래 걸립니다: {e}"
        ) from e

    if resp.status_code == 401:
        raise DiffusersUnavailable("GPU 서버 인증 실패 — GPU_SERVER_TOKEN 을 확인하세요")
    if resp.status_code == 503:
        # VRAM 부족. 이 장은 건너뛰고 다음을 시도한다.
        log.warning(f"  GPU 서버 혼잡: {resp.text[:120]}")
        return None
    if not resp.ok:
        log.warning(f"  GPU 서버 오류 [{resp.status_code}]: {resp.text[:150]}")
        return None

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return out_path
    except OSError as e:
        log.warning(f"  이미지 저장 실패: {e}")
        return None


def generate(
    prompt: str,
    negative: str,
    out_path: Path,
    *,
    width: int | None = None,
    height: int | None = None,
) -> Path | None:
    """이미지 한 장을 만들어 out_path 에 저장하고 그 경로를 돌려준다.

    DIFFUSERS_ENDPOINT 가 설정돼 있으면 원격 GPU 서버에 맡기고, 없으면
    이 컴퓨터에서 직접 만든다. 호출부는 어느 쪽인지 알 필요가 없다.

    width/height 를 주면 설정값 대신 그 크기로 만든다. Visual Plan 이
    종횡비를 확정하므로(2단계), 생성 후 크롭하지 않고 처음부터 그 비율로 뽑는다.
    생성 뒤에 자르면 구도가 깨진다.

    생성 자체가 실패하면 None 을 돌려준다(그 삽화만 건너뛴다).
    파이프라인을 아예 쓸 수 없으면 DiffusersUnavailable 을 올린다
    (나머지 삽화도 마찬가지일 테니 호출부가 루프를 멈춘다).
    """
    # SD 계열은 8의 배수만 받는다. 어긋나면 모델이 조용히 잘라 구도가 틀어진다.
    w = ((width or DIFFUSERS_WIDTH) // 8) * 8
    h = ((height or DIFFUSERS_HEIGHT) // 8) * 8

    if DIFFUSERS_ENDPOINT:
        return _generate_remote(prompt, negative, out_path, w, h)

    pipe = _get_pipe()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image = pipe(
            prompt=prompt,
            negative_prompt=negative or None,
            num_inference_steps=DIFFUSERS_STEPS,
            guidance_scale=_guidance(DIFFUSERS_MODEL),
            width=w,
            height=h,
        ).images[0]
        image.save(out_path)
        return out_path
    except Exception as e:
        log.warning(f"이미지 생성 실패: {e}")
        return None

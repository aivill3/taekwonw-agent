#!/usr/bin/env python
"""GPU 이미지 생성 서버 (독립 실행 파일).

공용 GPU 서버에 이 파일 하나만 두고 실행한다. 프로젝트를 import 하지 않으므로
Notion·Gemini 자격증명이 서버에 올라가지 않는다. 서버는 프롬프트를 받아
그림만 돌려준다.

    서버 :  python gpu_server.py
    로컬 :  ssh -N -L 8000:localhost:8000 사용자@서버
            .env 에 DIFFUSERS_ENDPOINT=http://localhost:8000

공용 서버라는 전제에서 정한 것들:

  127.0.0.1 에만 바인딩한다.
    0.0.0.0 으로 열면 같은 서버를 쓰는 모든 사람이 접근할 수 있고, 방화벽
    바깥으로 새어나갈 수도 있다. SSH 터널을 쓰면 포트를 열 필요가 없다.

  생성은 한 번에 하나만.
    동시 요청이 들어오면 VRAM 이 터진다. 락으로 직렬화한다. 대기가 생기지만
    OOM 으로 남의 작업까지 죽이는 것보다 낫다.

  유휴 시 모델을 내린다.
    SDXL 은 VRAM 을 8GB 넘게 쓴다. 공용 장비에서 아무것도 안 하면서 계속
    붙잡고 있으면 민폐다. IDLE_UNLOAD_SEC 동안 요청이 없으면 해제한다.

  토큰 인증(선택).
    터널만 쓰면 로컬 접근만 가능하지만, 같은 서버의 다른 계정도 localhost 로는
    접근할 수 있다. GPU_SERVER_TOKEN 을 걸어 두면 그것도 막힌다.

필요한 패키지:
    pip install fastapi uvicorn torch diffusers transformers accelerate safetensors
"""
import io
import os
import threading
import time

import torch
import uvicorn
from diffusers import AutoPipelineForText2Image
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

MODEL_ID = os.getenv("DIFFUSERS_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")
HOST = os.getenv("GPU_SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("GPU_SERVER_PORT", "8000"))
TOKEN = os.getenv("GPU_SERVER_TOKEN", "")

# 이 시간 동안 요청이 없으면 VRAM 을 반납한다. 0 이면 계속 붙잡고 있는다.
IDLE_UNLOAD_SEC = int(os.getenv("GPU_IDLE_UNLOAD_SEC", "600"))

MAX_SIDE = 1536  # 한 변 상한. 공용 장비에서 과도한 요청을 막는다.

app = FastAPI(title="taekwonw image server")

_pipe = None
_lock = threading.Lock()      # 생성 직렬화
_last_used = 0.0


def _guidance(model: str) -> float:
    """turbo/lightning 계열은 CFG 없이 학습돼 0.0 이 권장값이다."""
    name = model.lower()
    if any(k in name for k in ("turbo", "lightning", "lcm")):
        return 0.0
    return float(os.getenv("DIFFUSERS_GUIDANCE", "7.0"))


def _get_pipe():
    global _pipe, _last_used
    if _pipe is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        print(f"[load] {MODEL_ID} on {device} …", flush=True)
        pipe = AutoPipelineForText2Image.from_pretrained(MODEL_ID, torch_dtype=dtype)
        pipe = pipe.to(device)
        if hasattr(pipe, "safety_checker"):
            pipe.safety_checker = None
        pipe.set_progress_bar_config(disable=True)
        _pipe = pipe
        print("[load] ready", flush=True)
    _last_used = time.time()
    return _pipe


def _unload_watcher():
    """유휴 시간이 지나면 모델을 내려 VRAM 을 반납한다."""
    global _pipe
    if IDLE_UNLOAD_SEC <= 0:
        return
    while True:
        time.sleep(30)
        with _lock:
            if _pipe is not None and time.time() - _last_used > IDLE_UNLOAD_SEC:
                print("[unload] 유휴 — VRAM 반납", flush=True)
                _pipe = None
                torch.cuda.empty_cache()


class GenRequest(BaseModel):
    prompt: str
    negative: str = ""
    width: int = 1344
    height: int = 768
    steps: int = 30
    seed: int | None = None


def _check_token(authorization: str | None):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/health")
def health(authorization: str | None = Header(default=None)):
    _check_token(authorization)
    return {
        "ok": True,
        "model": MODEL_ID,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "loaded": _pipe is not None,
    }


@app.post("/generate")
def generate(req: GenRequest, authorization: str | None = Header(default=None)):
    _check_token(authorization)

    # 8의 배수로 맞추고 상한을 건다. SD 계열의 요구 조건이자 과부하 방지다.
    w = min(max(req.width, 256), MAX_SIDE) // 8 * 8
    h = min(max(req.height, 256), MAX_SIDE) // 8 * 8

    with _lock:                      # 동시 생성 금지
        pipe = _get_pipe()
        gen = None
        if req.seed is not None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            gen = torch.Generator(device=device).manual_seed(req.seed)
        try:
            image = pipe(
                prompt=req.prompt,
                negative_prompt=req.negative or None,
                num_inference_steps=req.steps,
                guidance_scale=_guidance(MODEL_ID),
                width=w,
                height=h,
                generator=gen,
            ).images[0]
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            raise HTTPException(status_code=503, detail="GPU 메모리 부족 — 잠시 후 재시도")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


if __name__ == "__main__":
    threading.Thread(target=_unload_watcher, daemon=True).start()
    print(f"[start] http://{HOST}:{PORT}  model={MODEL_ID}", flush=True)
    if HOST not in ("127.0.0.1", "localhost"):
        print("[warn] 외부에 노출됩니다. 공용 서버라면 SSH 터널을 쓰세요.", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

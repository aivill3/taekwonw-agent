"""GPU SDXL 서버 단독 테스트.

파이프라인을 거치지 않고 원격 GPU 서버만 두드린다. 삽화가 이상할 때
원인이 '프롬프트' 인지 '서버·모델' 인지 가르는 것이 목적이다.

전제:
  SSH 터널이 열려 있어야 한다. 별도 터미널에서 먼저 실행한다.

    ssh -N -L 8010:localhost:8010 gpu2

사용법:

    # 0) 서버 상태 — 어떤 체크포인트를 쓸 수 있는지 본다
    uv run python tools/test_gpu.py --health

    # 1) 기본 모델로 6장
    uv run python tools/test_gpu.py --repeat 6

    # 2) 체크포인트 지정
    uv run python tools/test_gpu.py --model juggernaut --repeat 6

    # 3) A/B 비교 — 같은 seed 로 여러 모델을 돌려 폴더를 나눠 저장
    uv run python tools/test_gpu.py --compare base,realvis,juggernaut --repeat 6

    # 4) 프롬프트를 직접
    uv run python tools/test_gpu.py -p "..." -n "..."

A/B 비교는 **같은 seed** 를 쓴다. 다른 seed 로 비교하면 모델 차이인지
seed 차이인지 구분할 수 없다.

의존성은 requests 와 Pillow 뿐이다. 프로젝트 설정(config/)을 읽지 않으므로
파이프라인이 망가진 상태에서도 이 파일만 따로 돌릴 수 있다.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import io
import os
import re
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL = "http://localhost:8010"
OUT_DIR = Path("data/gpu_test")

# 실측으로 확정한 기준 프롬프트 (2026-08-26, 6장 중 4장 사용 가능).
# prompt_compiler 의 상수와 같은 값이다. 여기를 바꾸면 그쪽도 바꿔야 한다.
DEFAULT_PROMPT = (
    "A single Korean teenage taekwondo athlete, "
    "one leg extended horizontally at shoulder height, supporting foot planted, "
    "white V-neck dobok with black collar and belt, bare feet, "
    "empty training hall with plain white wall and wooden floor, "
    "telephoto sports photograph, shallow depth of field"
)

# 해부학 용어를 넣지 않는다. 넣으면 오히려 사지가 망가진다(실측).
# 'sitting, kneeling, lying down' 이 서 있는 동작을 지키는 핵심이다.
DEFAULT_NEGATIVE = (
    "karate gi, cross-over lapel, judo, "
    "two people, second person, crowd, "
    "sitting, kneeling, lying down, "
    "shoes, sneakers, socks, "
    "logos, text, watermark, 3d render, cgi, illustration, blurry"
)

IMAGE_KEYS = ("image", "image_base64", "b64_json", "base64", "img", "data")

TOKEN_NAMES = (
    "GPU_SERVER_TOKEN", "GPU_TOKEN", "GPU_API_TOKEN", "SDXL_TOKEN",
    "IMAGE_API_TOKEN", "DIFFUSERS_TOKEN", "API_TOKEN",
)
TOKEN_PATTERN = re.compile(
    r"^(GPU|SDXL|IMAGE|DIFFUSERS)_?\w*(TOKEN|KEY|SECRET)$", re.I
)


def _log(msg: str = "") -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────
# 토큰
# ─────────────────────────────────────────────────────────────

def _from_dotenv() -> dict[str, str]:
    path = Path(".env")
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _from_settings() -> dict[str, str]:
    try:
        sys.path.insert(0, str(Path.cwd()))
        from config import settings  # type: ignore
    except Exception:
        return {}
    out: dict[str, str] = {}
    for name in dir(settings):
        if name.startswith("_") or not TOKEN_PATTERN.match(name):
            continue
        val = getattr(settings, name)
        if isinstance(val, str) and val:
            out[name] = val
    return out


def find_token(explicit: str | None) -> tuple[str | None, str]:
    if explicit:
        return explicit, "--token 인자"
    for name in TOKEN_NAMES:
        if os.environ.get(name):
            return os.environ[name], f"환경변수 {name}"
    for source, table in ((".env", _from_dotenv()),
                          ("config/settings.py", _from_settings())):
        for name in TOKEN_NAMES:
            if table.get(name):
                return table[name], f"{source} 의 {name}"
        for name, val in table.items():
            if TOKEN_PATTERN.match(name):
                return val, f"{source} 의 {name}"
    return None, "찾지 못함"


# ─────────────────────────────────────────────────────────────
# 응답 해석
# ─────────────────────────────────────────────────────────────

def _decode_b64(value: str) -> bytes | None:
    if value[:5].lower() == "data:" and "," in value[:64]:
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _extract_image(resp: requests.Response) -> bytes | None:
    """raw 바이트 / {"image": b64} / {"images": [b64]} 를 모두 처리한다."""
    if resp.headers.get("Content-Type", "").startswith("image/"):
        return resp.content
    try:
        data = resp.json()
    except ValueError:
        return resp.content if resp.content[:4] == b"\x89PNG" else None

    if isinstance(data, str):
        return _decode_b64(data)
    if isinstance(data, dict):
        for key in IMAGE_KEYS:
            val = data.get(key)
            if isinstance(val, str) and (out := _decode_b64(val)):
                return out
            if isinstance(val, list) and val and isinstance(val[0], str):
                if out := _decode_b64(val[0]):
                    return out
        for key in ("images", "artifacts", "output"):
            val = data.get(key)
            if isinstance(val, list) and val:
                first = val[0]
                if isinstance(first, str):
                    return _decode_b64(first)
                if isinstance(first, dict):
                    for k in IMAGE_KEYS:
                        if isinstance(first.get(k), str):
                            return _decode_b64(first[k])
        _log(f"  이미지를 못 찾았습니다. 응답 키: {list(data)}")
    return None


# ─────────────────────────────────────────────────────────────
# 서버 확인
# ─────────────────────────────────────────────────────────────

class ServerDown(Exception):
    """연결 자체가 안 되는 상태. 재시도해도 소용없다."""


def health(base: str, headers: dict) -> dict | None:
    """서버 생존과 사용 가능한 체크포인트를 확인한다.

    생성 루프에 들어가기 전에 한 번 부른다. 연결이 안 되는데 6번을
    반복하면 같은 오류만 여섯 줄 쌓인다.
    """
    url = f"{base.rstrip('/')}/health"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except requests.ConnectionError as e:
        raise ServerDown(str(e))
    except requests.RequestException as e:
        _log(f"  /health 실패: {type(e).__name__}: {e}")
        return None

    if resp.status_code in (401, 403):
        _log(f"  인증 실패 ({resp.status_code}). 토큰을 확인하세요.")
        return None
    if not resp.ok:
        _log(f"  /health HTTP {resp.status_code} — 구버전 서버일 수 있습니다.")
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _tunnel_help(base: str) -> None:
    _log(f"  연결할 수 없습니다: {base}")
    _log("  1) SSH 터널이 열려 있는지 확인하세요")
    _log("       ssh -N -L 8010:localhost:8010 gpu2")
    _log("  2) GPU 서버에서 프로세스가 살아 있는지 확인하세요")
    _log("       curl -s localhost:8010/health -H \"Authorization: Bearer $GPU_SERVER_TOKEN\"")


# ─────────────────────────────────────────────────────────────
# 생성
# ─────────────────────────────────────────────────────────────

_model_field_ok = True   # 서버가 model 필드를 받는가 (구버전이면 False 로 내려간다)


def generate(base: str, path: str, payload: dict, headers: dict,
             timeout: int) -> tuple[bytes | None, float, str]:
    """(이미지, 소요시간, 실제로 쓰인 모델) 을 돌려준다."""
    global _model_field_ok
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"

    if not _model_field_ok:
        payload = {k: v for k, v in payload.items() if k != "model"}

    started = time.perf_counter()
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.ConnectionError as e:
        raise ServerDown(str(e))
    except requests.Timeout:
        _log(f"  응답 시간 초과 ({timeout}초). --timeout 을 늘려 보세요.")
        return None, time.perf_counter() - started, ""

    elapsed = time.perf_counter() - started

    # 구버전 서버는 model 필드를 모른다. 한 번 걸러내고 재시도한다.
    if resp.status_code == 422 and "model" in payload and "model" in resp.text:
        _log("  서버가 model 필드를 받지 않습니다 — 체크포인트 전환 없이 진행합니다")
        _log("  (gpu_server.py 를 최신판으로 교체하면 전환이 가능합니다)")
        _model_field_ok = False
        return generate(base, path, payload, headers, timeout)

    if not resp.ok:
        _log(f"  HTTP {resp.status_code}")
        _log(f"  {resp.text[:300]}")
        if resp.status_code in (401, 403):
            _log("  인증 실패입니다. --token 을 확인하세요.")
        elif resp.status_code == 400:
            _log("  모델 이름을 확인하세요. --health 로 사용 가능한 값을 볼 수 있습니다.")
        return None, elapsed, ""

    used = resp.headers.get("X-Model", "")
    return _extract_image(resp), elapsed, used


def save(data: bytes, dest: Path) -> tuple[int, int] | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def run_model(model: str | None, args, headers: dict, prompt: str,
              negative: str, out_root: Path) -> tuple[int, int, float]:
    """한 체크포인트로 repeat 장 생성. (성공, 시도, 총 소요초)"""
    label = model or "(서버 기본)"
    out_dir = out_root / (model or "default")

    _log(f"── {label}")
    ok = 0
    total_sec = 0.0

    for i in range(args.repeat):
        seed = args.seed if args.seed is not None else 1000 + i
        payload = {
            "prompt": prompt,
            "negative": negative,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "seed": seed,
        }
        if args.guidance is not None:
            payload["guidance"] = args.guidance
        if model:
            payload["model"] = model

        data, elapsed, used = generate(
            args.url, args.path, payload, headers, args.timeout
        )
        total_sec += elapsed
        if not data:
            _log(f"   [{i + 1}/{args.repeat}] seed={seed} 실패 ({elapsed:.1f}초)")
            continue

        dest = out_dir / f"{seed}.png"
        size = save(data, dest)
        dim = f"{size[0]}x{size[1]}" if size else "?"
        tag = f" · 모델 {used}" if used and used != model else ""
        _log(f"   [{i + 1}/{args.repeat}] seed={seed}  {dest}  "
             f"({dim}, {elapsed:.1f}초{tag})")

        if size and (size[0], size[1]) != (args.width, args.height):
            _log(f"      경고: 요청 {args.width}x{args.height} ≠ 결과 {dim}")
        ok += 1

    return ok, args.repeat, total_sec


def main() -> int:
    ap = argparse.ArgumentParser(description="GPU SDXL 서버 테스트")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"기본 {DEFAULT_URL}")
    ap.add_argument("--path", default="/generate")
    ap.add_argument("--token", default=None, help="없으면 자동 탐색")
    ap.add_argument("--health", action="store_true", help="서버 상태만 보고 종료")
    ap.add_argument("--model", default=None,
                    help="체크포인트 이름 (base / realvis / juggernaut …)")
    ap.add_argument("--compare", default=None,
                    help="쉼표로 구분한 여러 모델을 같은 seed 로 비교")
    ap.add_argument("-p", "--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("-n", "--negative", default=DEFAULT_NEGATIVE)
    ap.add_argument("--width", type=int, default=1344)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--guidance", type=float, default=None,
                    help="생략하면 서버의 모델별 기본값을 쓴다")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--repeat", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    token, source = find_token(args.token)
    if token:
        masked = f"{token[:4]}…{token[-4:]}" if len(token) > 10 else "****"
        _log(f"토큰   : {masked}  ({source})")
    else:
        _log("토큰   : 없음 (인증이 걸려 있으면 401 이 납니다)")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # 생성 전에 서버부터 확인한다. 연결이 안 되는데 6번 반복할 이유가 없다.
    try:
        info = health(args.url, headers)
    except ServerDown:
        _tunnel_help(args.url)
        return 1

    available: list[str] = []
    if info:
        available = info.get("models") or []
        _log(f"서버   : {info.get('gpu') or info.get('device')}  "
             f"· 올라온 모델 {info.get('loaded') or '(없음)'}")
        if available:
            _log(f"체크포인트: {', '.join(available)}  (기본 {info.get('default')})")
    if args.health:
        return 0

    models: list[str | None]
    if args.compare:
        models = [m.strip() for m in args.compare.split(",") if m.strip()]
    elif args.model:
        models = [args.model]
    else:
        models = [None]

    for m in models:
        if m and available and m not in available:
            _log(f"경고   : '{m}' 은 서버 목록에 없습니다 — {available}")

    _log()
    _log(f"크기   : {args.width}x{args.height} · steps {args.steps} · "
         f"cfg {args.guidance if args.guidance is not None else '(서버 기본)'}")
    _log(f"[pos]  : {args.prompt}")
    _log(f"[neg]  : {args.negative}")
    _log()

    out_root = Path(args.out) / time.strftime("%m%d_%H%M")
    results: list[tuple[str, int, int, float]] = []

    for m in models:
        try:
            ok, tried, sec = run_model(
                m, args, headers, args.prompt, args.negative, out_root
            )
        except ServerDown:
            _tunnel_help(args.url)
            return 1
        results.append((m or "(서버 기본)", ok, tried, sec))
        _log()

    _log("── 결과")
    for name, ok, tried, sec in results:
        avg = sec / tried if tried else 0
        _log(f"   {name:16s} {ok}/{tried}장  ·  장당 평균 {avg:.1f}초")
    _log(f"   저장 위치: {out_root}")
    if len(results) > 1:
        _log()
        _log("   같은 seed 로 뽑았으므로 폴더별 같은 파일명끼리 비교하면 됩니다.")

    return 0 if any(ok for _, ok, _, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
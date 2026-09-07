"""검수를 마친 사진을 1000x1000 정사각으로 자른다.

왜 중앙 크롭이 아닌가
-------------------
4032x3024 를 정사각으로 자르면 가로 25%가 날아간다. 중앙을 기준으로
자르면 양옆이 잘리는데, 삽화로 쓰는 사진에서는 그 자리에 피사체가
있는 경우가 많다.

    발차기 사진   뻗은 발이 프레임 가장자리에 있으면 발이 잘린다
    겨루기 사진   마주 선 두 사람 중 한 명이 잘린다
    단체 수련     양끝 아이들이 사라진다

그래서 어디를 남길지 사진마다 정한다.

어떻게 정하나
-----------
1. 얼굴을 찾는다 (YuNet, OpenCV 내장 DNN 검출기)
   찾으면 얼굴들을 모두 담는 위치로 창을 옮긴다. 태권도 사진은
   대부분 사람이 주인공이므로 이것이 가장 잘 맞는다.

   Haar cascade 를 쓰다가 바꿨다. 실측에서 계단 난간과 청바지를
   얼굴로 잡아 창을 빈 코트 쪽으로 끌고 갔다. 엉뚱한 것을 잡는
   쪽이 아무것도 못 잡는 쪽보다 나쁘다. 마스크나 헤드기어를 쓴
   얼굴도 놓쳤는데, 태권도 사진에서는 그것이 흔한 상황이다.

2. 못 찾으면 밝기 변화량(gradient)이 가장 많은 위치를 고른다
   빈 도장, 트로피, 접힌 도복처럼 사람이 없는 사진에서 쓴다.
   피사체는 배경보다 윤곽이 뚜렷해 변화량이 크다.

   다만 순수하게 변화량만 보면 창이 배경 쪽으로 끌려갈 수 있다.
   벽돌 벽이나 나뭇잎처럼 잔무늬가 많은 곳이 점수가 높기 때문이다.
   촬영자는 대개 피사체를 가운데 두므로 중앙에 가중치를 준다.

3. 짧은 변이 목표 크기보다 작으면 건너뛴다
   675x1024 를 정사각으로 자르면 675x675 다. 이것을 1000 으로 늘리면
   흐려진다. 없는 화소를 만들어낼 수는 없다.

안전장치
-------
원본을 덮어쓰지 않는다. 결과는 --out 폴더에 따로 쌓이고, 눈으로 확인한
뒤에 옮기면 된다. 자동 크롭은 실패할 수 있고, 실패한 것을 되돌릴 수
없으면 곤란하다.

사용법
-----
    python scripts/crop_stock.py                          # data/stock → data/stock_cropped
    python scripts/crop_stock.py --size 1000
    python scripts/crop_stock.py --mark                   # 잘릴 범위를 표시한 미리보기
    python scripts/crop_stock.py --max-upscale 1.5        # 1.5배까지 확대 허용
    python scripts/crop_stock.py --top-shift 0.05         # 세로 사진 창을 5% 위로
    python scripts/crop_stock.py --dir data/stock_pending/kids
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageFilter, ImageOps  # noqa: E402

try:
    from config.settings import DATA_DIR
except ImportError:  # 프로젝트 밖에서 단독 실행할 때
    DATA_DIR = Path("data")

EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# 중앙 가중치. 클수록 창이 가운데로 끌린다.
#
# 0 이면 순수하게 변화량만 본다. 그러면 벽돌 벽이나 나뭇잎 같은
# 잔무늬 배경이 피사체보다 높은 점수를 받아 창이 엉뚱한 데로 간다.
# 1 이면 사실상 중앙 크롭이다. 0.35 는 실측으로 정한 값이 아니라
# 두 실패 양상 사이의 절충이므로, 결과를 보고 조정해도 된다.
CENTER_BIAS = 0.35

# 얼굴 아래로 남길 여백. 얼굴 높이의 배수.
#
# 얼굴만 딱 맞게 자르면 몸이 잘린다. 태권도 사진은 자세가 중요하므로
# 아래쪽에 자리를 준다. 다만 2.5 로 두었더니 창이 너무 내려가 이마와
# 눈이 잘렸다. 여백보다 얼굴이 온전한 것이 먼저다.
FACE_MARGIN = 1.2

# 얼굴 높이가 사진 높이의 이 비율보다 작으면 배경 인물로 본다.
#
# 경기 사진에는 관중과 대기 선수가 함께 찍힌다. 이들의 얼굴은 잘
# 잡히는 반면 정작 주인공은 헤드기어를 쓰거나 뒤를 보고 있어 안
# 잡힌다. 그대로 두면 창이 배경 쪽으로 끌려가 주인공이 잘린다.
#
# 실측: 900x600 경기 사진에서 관중 얼굴은 높이의 3~4%, 인물 사진의
# 주인공은 10% 이상이었다. 그 사이에 선을 긋는다.
MIN_FACE_RATIO = 0.07

# YuNet 검출 신뢰도 하한. 낮추면 오검출이 늘어난다.
FACE_SCORE = 0.7

# 검출용으로 줄일 크기. 원본 그대로 넣으면 느리고, YuNet 은 작은
# 입력에서도 잘 찾는다. 좌표는 원본 배율로 되돌린다.
DETECT_MAX_SIDE = 640

MODEL_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
             "models/face_detection_yunet/face_detection_yunet_2023mar.onnx")
MODEL_PATH = DATA_DIR / "models" / "face_detection_yunet_2023mar.onnx"


def ensure_model() -> Path | None:
    """YuNet 모델을 준비한다. 없으면 내려받는다."""
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 100_000:
        return MODEL_PATH

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"[모델] YuNet 을 내려받습니다 → {MODEL_PATH}")
    try:
        import urllib.request
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    except Exception as exc:
        print(f"  ! 내려받기 실패: {exc}")
        return None

    # git-lfs 저장소라 경로에 따라 실제 파일이 아니라 포인터 텍스트가
    # 온다. raw.githubusercontent.com 은 131바이트짜리 안내문을 준다.
    # 그대로 두면 나중에 ONNX 파싱 오류로 터지므로 여기서 걸러낸다.
    size = MODEL_PATH.stat().st_size
    if size < 100_000:
        head = MODEL_PATH.read_bytes()[:40]
        MODEL_PATH.unlink(missing_ok=True)
        if b"git-lfs" in head:
            print("  ! 실제 모델이 아니라 git-lfs 포인터를 받았습니다.")
        print(f"  ! 파일이 너무 작습니다 ({size}바이트). 직접 받아 두세요:")
        print(f"    {MODEL_URL}")
        print(f"    → {MODEL_PATH}")
        return None

    print(f"  받았습니다 ({size / 1024:.0f}KB)")
    return MODEL_PATH


def load_detector():
    """YuNet 검출기를 만든다. 준비 못 하면 None."""
    if not hasattr(cv2, "FaceDetectorYN"):
        print("! 이 OpenCV 버전에는 FaceDetectorYN 이 없습니다 (4.5.4+ 필요).")
        return None

    path = ensure_model()
    if path is None:
        return None

    try:
        return cv2.FaceDetectorYN.create(
            str(path), "", (320, 320), FACE_SCORE, 0.3, 5000)
    except cv2.error as exc:
        print(f"! 모델을 열지 못했습니다: {exc}")
        return None


def find_faces(bgr, detector) -> list[tuple[int, int, int, int]]:
    """(x, y, w, h) 목록을 낸다."""
    if detector is None:
        return []

    h, w = bgr.shape[:2]
    scale = min(1.0, DETECT_MAX_SIDE / max(h, w))
    if scale < 1.0:
        small = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
    else:
        small = bgr

    sh, sw = small.shape[:2]
    detector.setInputSize((sw, sh))
    try:
        _, found = detector.detect(small)
    except cv2.error:
        return []
    if found is None:
        return []

    out = []
    inv = 1.0 / scale if scale else 1.0
    for row in found:
        x, y, fw, fh = row[:4]
        out.append((int(x * inv), int(y * inv), int(fw * inv), int(fh * inv)))
    return out


def main_faces(faces: list, img_h: int) -> list:
    """배경 인물을 걸러낸다."""
    return [f for f in faces if f[3] >= img_h * MIN_FACE_RATIO]


def face_range(faces: list, w: int, h: int, side: int) -> tuple | None:
    """얼굴을 모두 담는 창 위치의 허용 범위를 낸다.

    담을 수 없으면 None. 이 경우 제약 없이 변화량으로만 고른다.
    """
    if not faces:
        return None

    x0 = min(f[0] for f in faces)
    y0 = min(f[1] for f in faces)
    x1 = max(f[0] + f[2] for f in faces)
    y1 = max(f[1] + f[3] for f in faces)

    pad = side * 0.04
    if (x1 - x0) + 2 * pad > side or (y1 - y0) + 2 * pad > side:
        return None

    left_lo = max(0.0, min(w - side, x1 + pad - side))
    left_hi = min(float(w - side), max(0.0, x0 - pad))
    top_lo = max(0.0, min(h - side, y1 + pad - side))
    top_hi = min(float(h - side), max(0.0, y0 - pad))

    if left_lo > left_hi or top_lo > top_hi:
        return None
    return left_lo, left_hi, top_lo, top_hi


def choose_window(gray: np.ndarray, faces: list, side: int,
                  top_shift: float = 0.0) -> tuple[int, int, str]:
    """창 위치를 고른다. (left, top, 방식)

    변화량이 기본이고, 얼굴은 탐색 범위를 좁히는 제약으로만 쓴다.

    얼굴을 기준점으로 삼던 방식은 두 가지로 실패했다. 배경 관중만
    잡히면 창이 그쪽으로 끌려갔고, 주인공이 헤드기어를 쓰면 아예
    아무 정보도 얻지 못했다. 변화량은 그 두 경우에 모두 동작한다.
    피사체는 배경보다 윤곽이 뚜렷하기 때문이다.

    그래서 순서를 뒤집었다. 어디를 볼지는 변화량이 정하고, 얼굴은
    '적어도 여기는 잘리면 안 된다'는 하한으로만 관여한다.
    """
    h, w = gray.shape
    kept = main_faces(faces, h)
    bounds = face_range(kept, w, h, side)

    small = cv2.resize(gray, (max(1, w // 4), max(1, h // 4)),
                       interpolation=cv2.INTER_AREA)
    gx = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3)
    integral = cv2.integral(cv2.magnitude(gx, gy))

    sh, sw = small.shape
    s_side = max(1, min(sh, sw))
    ratio_x, ratio_y = sw / w, sh / h

    horizontal = w >= h
    span = (w - side) if horizontal else (h - side)
    if bounds:
        lo, hi = (bounds[0], bounds[1]) if horizontal else (bounds[2], bounds[3])
    else:
        lo, hi = 0.0, float(span)

    best, best_off = -1.0, lo
    steps = 60
    for i in range(steps + 1):
        off = lo + (hi - lo) * i / steps
        if horizontal:
            sx, sy = int(off * ratio_x), 0
        else:
            sx, sy = 0, int(off * ratio_y)
        sx = int(np.clip(sx, 0, sw - s_side))
        sy = int(np.clip(sy, 0, sh - s_side))

        total = (integral[sy + s_side, sx + s_side] - integral[sy, sx + s_side]
                 - integral[sy + s_side, sx] + integral[sy, sx])

        # 창 중심이 사진 중심에서 얼마나 떨어졌는지 (0=중앙, 1=끝)
        center = (off + side / 2) / (w if horizontal else h)
        center_off = abs(center - 0.5) * 2
        score = float(total) * (1.0 - CENTER_BIAS * center_off)

        if score > best:
            best, best_off = score, off

    if horizontal:
        left, top = best_off, (h - side) / 2
    else:
        left, top = (w - side) / 2, best_off
        # 세로 사진에서 창을 위로 당긴다.
        #
        # 변화량은 몸통·팔처럼 윤곽이 몰린 가운데를 좋아해서 창이
        # 내려가고, 머리 끝이 잘리는 일이 생긴다. 고개를 젖혔거나
        # 헤드기어를 쓰면 얼굴 검출도 못 잡아 막을 방법이 없다.
        # 인물 사진은 머리가 위에 있으므로 위쪽이 안전한 방향이다.
        if top_shift:
            top -= side * top_shift
    if bounds:
        left = float(np.clip(left, bounds[0], bounds[1]))
        top = float(np.clip(top, bounds[2], bounds[3]))
    left = int(np.clip(left, 0, w - side))
    top = int(np.clip(top, 0, h - side))

    if kept and bounds:
        method = f"변화량+얼굴 {len(kept)}"
    elif kept:
        method = f"변화량 (얼굴 {len(kept)} 담을 수 없음)"
    elif faces:
        method = f"변화량 (배경 얼굴 {len(faces)} 무시)"
    else:
        method = "변화량"
    return left, top, method


def process(path: Path, size: int, detector, mark: bool, max_upscale: float,
            top_shift: float = 0.0):
    """(결과 이미지, 방식) 을 낸다. 처리 못 하면 (None, 사유)."""
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:
        return None, f"열기 실패 ({exc})"

    w, h = img.size
    side = min(w, h)
    upscale = size / side
    if upscale > max_upscale:
        return None, f"짧은 변 {side}px — {upscale:.2f}배 확대 필요"

    array = np.array(img)
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    bgr = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)

    faces = find_faces(bgr, detector)
    left, top, method = choose_window(gray, faces, side, top_shift)

    if upscale > 1.0:
        method += f" ↑{upscale:.2f}배"

    if mark:
        # 자를 자리를 원본 위에 표시한다. 자동 판단을 검증하기 위한 것.
        marked = array.copy()
        cv2.rectangle(marked, (left, top), (left + side, top + side), (255, 60, 60), 8)
        for (fx, fy, fw, fh) in faces:
            cv2.rectangle(marked, (fx, fy), (fx + fw, fy + fh), (60, 200, 60), 4)
        out = Image.fromarray(marked)
        out.thumbnail((900, 900), Image.LANCZOS)
        return out, method

    cropped = img.crop((left, top, left + side, top + side))
    if side != size:
        cropped = cropped.resize((size, size), Image.LANCZOS)
        if upscale > 1.0:
            # 확대하면 가장자리가 뭉개진다. 약한 언샤프로 되살린다.
            #
            # 1.5배 이하에서는 이것만으로 눈에 띄는 차이가 거의 없다.
            # 강하게 걸면 윤곽에 흰 테가 생기므로 percent 를 낮게 둔다.
            cropped = cropped.filter(
                ImageFilter.UnsharpMask(radius=1.2, percent=70, threshold=3))
    return cropped, method


def main() -> int:
    parser = argparse.ArgumentParser(description="검수 완료 사진을 정사각으로 크롭")
    parser.add_argument("--dir", default=str(DATA_DIR / "stock"))
    parser.add_argument("--out", default=str(DATA_DIR / "stock_cropped"))
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--mark", action="store_true",
                        help="자를 자리를 표시한 미리보기를 만든다 (크롭하지 않음)")
    parser.add_argument("--max-upscale", type=float, default=1.0,
                        help="이 배율까지는 확대해서 처리한다 (기본 1.0 = 확대 안 함)")
    parser.add_argument("--top-shift", type=float, default=0.0,
                        help="세로 사진에서 창을 위로 당긴다. 창 크기 대비 비율 "
                             "(예: 0.05 = 5%%). 머리가 잘릴 때 쓴다")
    parser.add_argument("--quality", type=int, default=90)
    args = parser.parse_args()

    root, out_dir = Path(args.dir), Path(args.out)
    if not root.is_dir():
        print(f"폴더가 없습니다: {root}")
        return 1

    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and p.suffix.lower() in EXTS)
    if not files:
        print(f"{root} 에 이미지가 없습니다.")
        return 1

    detector = load_detector()
    if detector is None:
        print("  얼굴 검출 없이 변화량 방식만 씁니다.")

    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    by_method: dict[str, int] = {}
    skipped: list[tuple[str, str]] = []

    for path in files:
        result, method = process(path, args.size, detector, args.mark,
                                 args.max_upscale, args.top_shift)
        if result is None:
            skipped.append((path.name, method))
            continue

        suffix = "_mark" if args.mark else ""
        dest = out_dir / f"{path.stem}{suffix}.jpg"
        result.save(dest, "JPEG", quality=args.quality)
        done += 1
        key = "얼굴" if method.startswith("얼굴") else method
        by_method[key] = by_method.get(key, 0) + 1
        print(f"  {path.name:<44} {method}")

    print(f"\n{done}/{len(files)}장 처리 → {out_dir}")
    for method, count in sorted(by_method.items(), key=lambda x: -x[1]):
        print(f"  {method} {count}장")

    if skipped:
        print(f"\n건너뜀 {len(skipped)}장 — 확대 한도({args.max_upscale:.2f}배)를 넘습니다")
        for name, reason in skipped:
            print(f"  {name:<44} {reason}")
        print("  --max-upscale 을 올리면 처리되지만 그만큼 흐려집니다.")
        print("  1.5배 정도까지는 육안으로 거의 차이가 없습니다.")

    if args.mark:
        print("\n빨간 사각형이 잘릴 범위, 초록이 검출된 얼굴입니다.")
        print("자동 판단이 틀린 사진을 찾은 뒤에 --mark 없이 다시 실행하세요.")
    else:
        print(f"\n원본은 {root} 에 그대로 있습니다.")
        print("결과를 확인하고 문제없으면 옮기세요.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
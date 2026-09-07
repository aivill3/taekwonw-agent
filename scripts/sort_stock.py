"""무료가 아닌 스톡 사진을 검색 대상에서 빼낸다.

왜 필요한가
----------
`data/stock` 이 CLIP 인덱스의 대상 폴더다. 여기 들어 있는 사진은 검색에
잡히고 그대로 블로그에 실린다. 라이선스를 사지 않은 사진이 섞여 있으면
발행된 뒤에 회수하기 어렵다. 인덱싱 대상에서 아예 빼는 것이 안전하다.

  실측(2026-09-03): 인덱스 등록 29장 중 18장이 유료였다.
  Unsplash+ 15, Shutterstock 3.

파일명으로 판정한다
-----------------
스톡 사이트마다 내려받는 파일명 규칙이 있어 대개 구분된다. 다만 이름은
바뀔 수 있으므로 자동 판정은 참고이고 최종 확인은 사람이 한다. 그래서
기본이 미리보기이고 --apply 를 붙여야 실제로 옮긴다.

`premium_photo-` 를 유료로 넣은 이유
---------------------------------
Unsplash 는 무료로 알려져 있지만 Unsplash+ 는 유료 구독이고, 그 이미지의
파일명이 `premium_photo-` 로 시작한다. 일반 무료는 `photo-` 다. 접두사
하나 차이라 눈으로는 거의 구분되지 않는다. 실제로 15장이 이 경로로
검색 대상에 들어와 있었다.

사용법
-----
    python scripts/sort_stock.py                          # 미리보기
    python scripts/sort_stock.py --list                   # 전체 목록
    python scripts/sort_stock.py --apply                  # 유료만 옮긴다
    python scripts/sort_stock.py --include-unknown --apply # 출처 미상까지
    python scripts/sort_stock.py --restore --apply        # 되돌린다

옮긴 뒤에는 반드시 인덱스를 다시 만든다. 안 하면 폴더에서 뺀 사진이
인덱스에 남아 계속 검색된다.

    python scripts/build_index.py --dir data/stock --out data/index
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from config.settings import DATA_DIR
except ImportError:  # 프로젝트 밖에서 단독 실행할 때
    DATA_DIR = Path("data")

STOCK_DIR = DATA_DIR / "stock"
LICENSED_DIR = DATA_DIR / "stock_licensed"

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}

# 유료. 접두사로 판정한다.
PAID_PREFIXES = {
    "istockphoto-": "iStock",
    "stock-photo-": "Shutterstock",
    "gettyimages-": "Getty Images",
    "adobestock-": "Adobe Stock",
    "premium_photo-": "Unsplash+ (유료 구독)",
    "depositphotos-": "Depositphotos",
    "alamy-": "Alamy",
    "123rf-": "123RF",
    "dreamstime-": "Dreamstime",
}

# 무료. 접두사 또는 접미사.
FREE_PREFIXES = {
    "pexels-": "Pexels",
    "photo-": "Unsplash",
    "pixabay-": "Pixabay",
}
FREE_SUFFIXES = {
    "-unsplash": "Unsplash",
}


def classify(name: str) -> tuple[str, str]:
    """(구분, 출처) 를 낸다. 구분은 paid / free / unknown."""
    lower = name.lower()

    # 유료를 먼저 본다. `premium_photo-` 가 `photo-` 보다 먼저 걸려야 한다.
    for prefix, label in PAID_PREFIXES.items():
        if lower.startswith(prefix):
            return "paid", label

    for prefix, label in FREE_PREFIXES.items():
        if lower.startswith(prefix):
            return "free", label

    stem = Path(lower).stem
    for suffix, label in FREE_SUFFIXES.items():
        if stem.endswith(suffix):
            return "free", label

    return "unknown", "출처 미상"


def scan(folder: Path) -> dict[str, list[tuple[Path, str]]]:
    buckets: dict[str, list[tuple[Path, str]]] = {"paid": [], "free": [], "unknown": []}
    if not folder.is_dir():
        return buckets
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in EXTS:
            kind, label = classify(path.name)
            buckets[kind].append((path, label))
    return buckets


def summarize(buckets: dict[str, list[tuple[Path, str]]]) -> None:
    total = sum(len(v) for v in buckets.values())
    print(f"  전체 {total}장")
    for kind, title in (("paid", "유료"), ("free", "무료"), ("unknown", "출처 미상")):
        items = buckets[kind]
        if not items:
            continue
        by_label: dict[str, int] = {}
        for _, label in items:
            by_label[label] = by_label.get(label, 0) + 1
        detail = ", ".join(f"{k} {v}" for k, v in sorted(by_label.items()))
        print(f"    {title} {len(items)}장 — {detail}")


def move_all(pairs: list[tuple[Path, str]], dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path, _ in pairs:
        target = dest / path.name
        if target.exists():
            print(f"    건너뜀 (같은 이름 존재): {path.name}")
            continue
        shutil.move(str(path), str(target))
        moved += 1
    return moved


def cmd_restore(args: argparse.Namespace) -> int:
    buckets = scan(LICENSED_DIR)
    free = buckets["free"]
    print(f"{LICENSED_DIR} 안에서 무료로 판정되는 사진")
    summarize(buckets)

    if not free:
        print("\n되돌릴 것이 없습니다.")
        return 0

    print(f"\n{len(free)}장을 {STOCK_DIR} 로 되돌립니다.")
    for path, label in free:
        print(f"    {path.name}  ({label})")

    if not args.apply:
        print("\n미리보기입니다. --apply 를 붙이면 실제로 옮깁니다.")
        return 0

    moved = move_all(free, STOCK_DIR)
    print(f"\n{moved}장을 되돌렸습니다.")
    print("인덱스를 다시 만드세요:")
    print("    python scripts/build_index.py --dir data/stock --out data/index")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="유료 스톡 사진 분리")
    parser.add_argument("--apply", action="store_true", help="실제로 옮긴다")
    parser.add_argument("--include-unknown", action="store_true",
                        help="출처 미상도 함께 옮긴다")
    parser.add_argument("--list", action="store_true", help="파일을 하나씩 보여준다")
    parser.add_argument("--restore", action="store_true",
                        help="stock_licensed 안의 무료 사진을 되돌린다")
    args = parser.parse_args()

    if args.restore:
        return cmd_restore(args)

    if not STOCK_DIR.is_dir():
        print(f"폴더가 없습니다: {STOCK_DIR}")
        return 1

    buckets = scan(STOCK_DIR)
    print(f"{STOCK_DIR}")
    summarize(buckets)

    if args.list:
        for kind, title in (("paid", "유료"), ("unknown", "출처 미상"), ("free", "무료")):
            if not buckets[kind]:
                continue
            print(f"\n[{title}]")
            for path, label in buckets[kind]:
                print(f"    {path.name}  ← {label}")

    targets = list(buckets["paid"])
    if args.include_unknown:
        targets += buckets["unknown"]

    if not targets:
        print("\n옮길 것이 없습니다.")
        return 0

    remaining = sum(len(v) for v in buckets.values()) - len(targets)
    print(f"\n{len(targets)}장을 {LICENSED_DIR} 로 옮깁니다. 남는 사진 {remaining}장.")

    if not args.include_unknown and buckets["unknown"]:
        print(f"  출처 미상 {len(buckets['unknown'])}장은 그대로 둡니다.")
        print("  라이선스를 확인할 수 없다면 --include-unknown 을 붙이세요.")

    if not args.apply:
        print("\n미리보기입니다. --apply 를 붙이면 실제로 옮깁니다.")
        return 0

    moved = move_all(targets, LICENSED_DIR)
    print(f"\n{moved}장을 옮겼습니다. 검색 대상은 {remaining}장입니다.")
    print("\n인덱스를 반드시 다시 만드세요. 안 하면 옮긴 사진이 계속 검색됩니다:")
    print("    python scripts/build_index.py --dir data/stock --out data/index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
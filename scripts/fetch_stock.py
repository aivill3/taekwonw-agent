"""Pexels 에서 스톡 사진을 받아 검증 대기 폴더에 넣는다.

왜 필요한가
----------
쓸 수 있는 사진이 9장이다. 하루 4편 × 4자리 = 16장을 쓰는데 재고가
그 절반이라, 폴백이 같은 사진을 계속 돌려쓴다.

  실측(2026-09-03): data/stock 40개 중 이미지는 29개(나머지는 .mhtml),
  그 중 유료 18장을 빼고 영상 캡처 2장과 중복 1장을 빼면 9장이 남는다.

라이선스
-------
Pexels 만 받는다. 상업적 이용과 수정이 허용되고 출처 표기 의무도 없다.
그래도 사진마다 촬영자와 원본 URL 을 stock_sources.json 에 남긴다.
나중에 "이 사진 어디서 왔나" 를 물었을 때 답할 수 있어야 한다.

어디에 받나
----------
data/stock_pending/<카테고리>/ 에 받는다. data/stock 에 바로 넣지 않는다.

  data/stock 은 CLIP 인덱스의 대상 폴더다. 검증하지 않은 사진을 여기
  넣으면 그대로 검색에 잡혀 블로그에 실린다. 종목이 틀린 사진(가라테,
  유도)을 거른 뒤에 옮겨야 한다.

    fetch_stock.py → stock_pending/ → verify_stock.py → stock/ → build_index.py

'karate' 를 질의에 넣지 않는 이유
------------------------------
생성 프롬프트의 negative 에서 가라테 gi 를 빼고 있는데, 스톡에 가라테
사진이 섞이면 그 노력이 무의미해진다. 그래도 'martial arts' 계열 질의는
가라테를 상당수 돌려주므로 verify_stock.py 의 검증이 여전히 필요하다.

사용법
-----
    python scripts/fetch_stock.py --survey          # 받지 않고 재고만 센다
    python scripts/fetch_stock.py                   # 전체 수집
    python scripts/fetch_stock.py --category space  # 한 카테고리만
    python scripts/fetch_stock.py --query "gym mat" --category space
    python scripts/fetch_stock.py --audit           # 출처를 모르는 파일 찾기

API 키는 https://www.pexels.com/api/ 에서 무료로 발급받아 .env 에 넣는다.

    PEXELS_API_KEY=...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

try:
    from config.settings import DATA_DIR, KST
except ImportError:  # 프로젝트 밖에서 단독 실행할 때
    from datetime import timedelta, timezone
    DATA_DIR = Path("data")
    KST = timezone(timedelta(hours=9))

PENDING_DIR = DATA_DIR / "stock_pending"
SOURCES = DATA_DIR / "stock_sources.json"

API = "https://api.pexels.com/v1/search"

# 긴 변을 이 크기로 줄여 저장한다.
#
# 원본은 장당 2~5MB 다. 300장이면 1GB 가 넘어 저장소에 넣을 수 없다.
# 삽화는 Notion 본문에 들어가는 크기라 1600px 이면 충분하고,
# hybrid_image_agent._copy_stock 도 같은 값으로 줄인다.
MAX_SIDE = 1600

# 짧은 변이 이보다 작으면 받지 않는다.
#
# build_index.py 가 짧은 변 1024px 미만을 img2img 부적합으로 표시한다.
# 받기 전에 거르는 편이 낫다. API 응답에 width/height 가 들어 있어
# 내려받지 않고도 판정할 수 있다.
MIN_SHORT_SIDE = 1024

# ── 질의 세트 ────────────────────────────────────────────────────────
#
# build_index.py 의 PROBES 14개에 직접 대응시켰다. 수집 후 인덱싱하면
# 분포 리포트에서 어느 칸이 채워졌는지 바로 확인된다.
#
# target 배분의 근거:
#   태권도 전용 장면(겨루기·발차기·격파·심판)은 Pexels 재고가 얕다.
#   반면 종목 무관 장면(빈 수련장·트로피·장비·경기장)은 넉넉하고,
#   가라테 혼입 위험도 초상권 판단도 없다. 채울 수 있는 쪽을 채운다.
#
# category 는 폴더 이름이자 verify_stock.py 의 판정 강도 기준이 된다.
#   AUTO_PASS(space, gear, award, venue) — 인물이 없어 채점을 건너뛴다
#   STRICT(poomsae, misc) — 가라테 혼입 위험이 커 정식 채점한다

QUERIES: dict[str, dict] = {
    # ── 종목 무관. 재고가 넉넉하고 검증이 가볍다 ──
    "space": {
        "probe": "an empty training hall interior",
        "target": 50,
        "queries": [
            "empty gym interior", "martial arts dojo", "training mat floor",
            "empty sports hall", "gym wall mirror", "yoga studio empty",
            "wooden floor gymnasium",
        ],
    },
    "gear": {
        "probe": "a close-up of a colored belt / a white uniform dobok / protective gear",
        "target": 50,
        "queries": [
            "martial arts belt", "black belt close up", "white martial arts uniform",
            "folded uniform clothing", "kick pad training", "boxing headgear",
            "protective sports gear", "sports mouthguard",
        ],
    },
    "award": {
        "probe": "a trophy or medal ceremony",
        "target": 40,
        "queries": [
            "gold medal athlete", "sports podium ceremony", "trophy close up",
            "medal hanging ribbon", "winner celebration sport", "award ceremony stage",
        ],
    },
    "venue": {
        "probe": "(경기장 배경. PROBES 에는 없지만 폴백 자산으로 쓴다)",
        "target": 25,
        "queries": [
            "indoor sports arena", "sports competition crowd", "gymnasium wide shot",
        ],
    },
    # ── 인물이 있지만 종목 특정이 약하다 ──
    "feet": {
        "probe": "bare feet on a training mat",
        "target": 20,
        "queries": [
            "bare feet mat", "barefoot training floor", "feet on wooden floor",
        ],
    },
    "kids": {
        "probe": "a child practicing martial arts / students training together",
        "target": 40,
        "queries": [
            "kids martial arts class", "children taekwondo", "child white uniform training",
            "youth martial arts practice", "children exercise class", "kids sports team",
        ],
    },
    # ── 태권도 전용. 재고가 얕다 ──
    "sparring": {
        "probe": "two athletes sparring / a person performing a high kick",
        "target": 45,
        "queries": [
            "taekwondo", "taekwondo kick", "taekwondo sparring",
            "taekwondo competition", "high kick martial arts",
            "athlete kicking pad", "martial arts fighter kick",
        ],
    },
    "poomsae": {
        "probe": "a taekwondo poomsae form pose / hands or fists in a fighting stance",
        "target": 20,
        "queries": [
            "taekwondo stance", "martial arts form practice", "fighting stance pose",
            "martial arts training solo",
        ],
    },
    "misc": {
        "probe": "breaking a wooden board / a referee or judge at a competition",
        "target": 10,
        "queries": [
            "board breaking martial arts", "sports referee", "referee whistle game",
        ],
    },
}


def load_sources() -> dict:
    if not SOURCES.exists():
        return {}
    try:
        return json.loads(SOURCES.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  ! {SOURCES} 를 읽지 못했습니다. 빈 상태로 시작합니다.")
        return {}


def save_sources(data: dict) -> None:
    SOURCES.parent.mkdir(parents=True, exist_ok=True)
    SOURCES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def search(client: httpx.Client, query: str, per_page: int, page: int = 1) -> dict:
    resp = client.get(API, params={"query": query, "per_page": per_page, "page": page})
    resp.raise_for_status()
    return resp.json()


def download(client: httpx.Client, photo: dict, dest: Path) -> bool:
    """받아서 MAX_SIDE 로 줄여 저장한다."""
    url = photo["src"].get("large2x") or photo["src"]["original"]
    try:
        resp = client.get(url, timeout=60)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"      실패: {exc}")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)

    try:
        with Image.open(dest) as img:
            img = img.convert("RGB")
            if max(img.size) > MAX_SIDE:
                ratio = MAX_SIDE / max(img.size)
                size = (round(img.width * ratio), round(img.height * ratio))
                img = img.resize(size, Image.LANCZOS)
            img.save(dest, "JPEG", quality=88)
    except Exception as exc:  # 손상된 파일은 남기지 않는다
        print(f"      이미지 처리 실패, 삭제: {exc}")
        dest.unlink(missing_ok=True)
        return False

    return True


def cmd_survey(client: httpx.Client, plan: dict) -> int:
    """받지 않고 질의별 재고만 센다.

    목표 장수를 정하기 전에 실제 상한을 알아야 한다. Pexels 응답의
    total_results 로 확인할 수 있다.
    """
    print("질의별 재고 (받지 않음)\n")
    grand = 0
    for category, spec in plan.items():
        print(f"[{category}]  목표 {spec['target']}장")
        subtotal = 0
        for query in spec["queries"]:
            try:
                data = search(client, query, per_page=1)
            except httpx.HTTPError as exc:
                print(f"    {query:<34} 조회 실패 ({exc})")
                continue
            total = data.get("total_results", 0)
            subtotal += total
            print(f"    {query:<34} {total:>7,}")
            time.sleep(0.3)
        print(f"    {'합계(중복 포함)':<34} {subtotal:>7,}\n")
        grand += subtotal
    print(f"전체 합계 {grand:,} (질의 간 중복 포함, 실제 확보량은 이보다 훨씬 적음)")
    print("\n재고가 큰 카테고리는 목표를 올리고, 얕은 곳은 낮추는 것이 낫습니다.")
    return 0


def cmd_audit() -> int:
    """출처를 모르는 파일을 찾는다."""
    sources = load_sources()
    orphans: list[Path] = []
    for folder in (DATA_DIR / "stock", PENDING_DIR):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
                if path.name not in sources:
                    orphans.append(path)

    if not orphans:
        print("모든 파일의 출처가 기록되어 있습니다.")
        return 0

    print(f"출처 기록이 없는 파일 {len(orphans)}개")
    for path in orphans:
        print(f"    {path}")
    print("\n라이선스를 확인할 수 없다면 쓰지 않는 편이 안전합니다.")
    return 0


def fetch_category(client: httpx.Client, category: str, spec: dict,
                   sources: dict, per_query: int | None, dry_run: bool) -> int:
    target = spec["target"]
    queries = spec["queries"]
    quota = per_query or max(1, -(-target // len(queries)))  # 올림 나눗셈

    dest_dir = PENDING_DIR / category
    got = 0
    skipped_small = 0
    skipped_dup = 0

    # 질의끼리 결과가 겹친다. 실제 수집은 sources 에 기록하며 걸러지지만
    # dry-run 은 기록하지 않아 같은 사진을 여러 번 세게 된다. 미리보기
    # 숫자로 계획을 세우므로 여기서도 같은 기준으로 걸러야 한다.
    seen: set[str] = set()

    print(f"\n[{category}]  목표 {target}장, 질의당 {quota}장")

    for query in queries:
        if got >= target:
            break
        try:
            data = search(client, query, per_page=min(quota * 2, 80))
        except httpx.HTTPError as exc:
            print(f"  {query} — 조회 실패 ({exc})")
            continue

        taken = 0
        for photo in data.get("photos", []):
            if taken >= quota or got >= target:
                break

            name = f"pexels-{photo['id']}.jpg"
            if name in sources or name in seen:
                skipped_dup += 1
                continue
            seen.add(name)

            if min(photo.get("width", 0), photo.get("height", 0)) < MIN_SHORT_SIDE:
                skipped_small += 1
                continue

            if dry_run:
                taken += 1
                got += 1
                continue

            if not download(client, photo, dest_dir / name):
                continue

            sources[name] = {
                "source": "pexels",
                "photo_id": photo["id"],
                "photographer": photo.get("photographer", ""),
                "photographer_url": photo.get("photographer_url", ""),
                "url": photo.get("url", ""),
                "alt": photo.get("alt", ""),
                "license": "Pexels License (상업적 이용 가능, 출처 표기 불필요)",
                "category": category,
                "query": query,
                "fetched": datetime.now(KST).isoformat(timespec="seconds"),
            }
            taken += 1
            got += 1
            time.sleep(0.2)

        print(f"  {query:<34} +{taken}")

    note = []
    if skipped_dup:
        note.append(f"중복 {skipped_dup}")
    if skipped_small:
        note.append(f"해상도 미달 {skipped_small}")
    tail = f"  (건너뜀: {', '.join(note)})" if note else ""
    print(f"  → {got}/{target}장{tail}")
    return got


def main() -> int:
    parser = argparse.ArgumentParser(description="Pexels 스톡 수집")
    parser.add_argument("--survey", action="store_true", help="받지 않고 재고만 센다")
    parser.add_argument("--audit", action="store_true", help="출처 미기록 파일을 찾는다")
    parser.add_argument("--dry-run", action="store_true", help="받지 않고 몇 장 받을지만 본다")
    parser.add_argument("--category", help="한 카테고리만 (예: space)")
    parser.add_argument("--query", help="직접 지정한 질의 하나만. --category 필요")
    parser.add_argument("--per-query", type=int, help="질의당 장수 (기본: 목표÷질의수)")
    args = parser.parse_args()

    if args.audit:
        return cmd_audit()

    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        env = Path(".env")
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("PEXELS_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        print("PEXELS_API_KEY 가 없습니다.")
        print("https://www.pexels.com/api/ 에서 발급받아 .env 에 넣으세요.")
        print("    PEXELS_API_KEY=...")
        return 1

    plan = dict(QUERIES)
    if args.category:
        if args.category not in plan:
            print(f"모르는 카테고리: {args.category}")
            print(f"쓸 수 있는 것: {', '.join(plan)}")
            return 1
        plan = {args.category: plan[args.category]}
    if args.query:
        if not args.category:
            print("--query 는 --category 와 함께 써야 합니다. 받은 사진을 넣을 폴더가 필요합니다.")
            return 1
        plan[args.category] = {**plan[args.category], "queries": [args.query]}

    with httpx.Client(headers={"Authorization": key}, timeout=30) as client:
        if args.survey:
            return cmd_survey(client, plan)

        sources = load_sources()
        before = len(sources)
        total = 0
        try:
            for category, spec in plan.items():
                total += fetch_category(client, category, spec, sources,
                                        args.per_query, args.dry_run)
        finally:
            # 중간에 끊겨도 받은 것까지는 기록을 남긴다.
            if not args.dry_run and len(sources) > before:
                save_sources(sources)

    if args.dry_run:
        print(f"\n미리보기: {total}장을 받게 됩니다. --dry-run 을 빼면 실제로 받습니다.")
        return 0

    print(f"\n{total}장을 받아 {PENDING_DIR} 에 넣었습니다.")
    print("\n다음:")
    print("    python scripts/verify_stock.py init --dir data/stock_pending")
    print("    (CSV 작성 후)")
    print("    python scripts/verify_stock.py apply --dir data/stock_pending --apply")
    print("    python scripts/build_index.py --dir data/stock --out data/index")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
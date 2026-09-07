"""Openverse 에서 CC0 태권도 사진을 받아 검증 대기 폴더에 넣는다.

왜 Pexels 만으로는 부족한가
-------------------------
Pexels 300장을 받아 검수한 결과 49장만 남았다. 생존율 16%.

    질의에 'taekwondo' 포함    51%
    무술 일반 (martial arts~)  4~10%
    장면 묘사 (bare feet 등)   0%

'taekwondo' 질의는 재고가 4,345건이고 중복이 심해(sparring 카테고리에서
중복 40건) 더 짜내도 20~30장이 한계다. 100장을 넘기려면 다른 소스가
필요하다.

Openverse 는 무엇이 다른가
------------------------
검색 엔진이라 사진마다 라이선스가 다르다. 그래서 CC0 와 Public Domain
만 받는다. CC BY 는 상업 이용은 되지만 글마다 저작자·라이선스·원본
링크를 명시해야 해서 자동 발행에 붙이기 어렵고, BY-SA 는 동일조건
전파 조항이, ND 는 변경 금지 조항이 걸린다. 이 파이프라인은 이미지를
1600px 로 리사이즈하므로 ND 는 특히 위험하다.

품질 특성도 반대다.

    Pexels     검색어와 무관한 사진이 섞임 → 종목 판별이 문제
    Openverse  제목에 taekwondo 가 명시됨 → 종목은 거의 확실

대신 다른 문제가 있다. Wikimedia Commons 가 주력이라 '기록 사진'이
많다. 선수 실명이 붙은 인물 사진, 시장 접견 같은 의전 행사, 뉴스
아카이브의 흑백 사진들이다. 이런 것은 종목이 맞아도 무관한 기사에
삽화로 쓸 수 없다. 그래서 필터의 목적이 다르다.

    Pexels 검증     이게 태권도인가
    Openverse 필터  이걸 삽화로 써도 되나

사용법
-----
    python scripts/fetch_openverse.py --dry-run   # 몇 장 받을지 확인
    python scripts/fetch_openverse.py             # 수집
    python scripts/fetch_openverse.py --show-skip # 걸러진 이유까지 출력

API 키가 없어도 시간당 100회까지 익명 요청이 가능하다. 질의가 6개라
한도 안에 들어간다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
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

API = "https://api.openverse.org/v1/images/"

MAX_SIDE = 1600          # fetch_stock.py 와 같은 값
MIN_SHORT_SIDE = 1024    # build_index.py 의 img2img 기준

# CC0 와 Public Domain Mark 만. 이유는 위 docstring 참고.
LICENSES = "cc0,pdm"

QUERIES: dict[str, dict] = {
    "sparring": {
        "target": 25,
        "queries": ["taekwondo kyorugi", "taekwondo kick", "taekwondo competition"],
    },
    "kids": {
        "target": 20,
        "queries": ["taekwondo class", "children taekwondo"],
    },
    "poomsae": {
        "target": 10,
        "queries": ["taekwondo poomsae", "taekwondo training"],
    },
    "venue": {
        "target": 10,
        "queries": ["taekwondo championship", "dojang taekwondo"],
    },
}

# ── 제목 기반 사전 필터 ──────────────────────────────────────────────
#
# Wikimedia 기록 사진의 특성을 제목으로 걸러낸다. 받아서 눈으로 보는
# 것보다 싸고, 애초에 받지 않으면 검수 대상도 줄어든다.

# 의전·행사·인물 기록. 맥락이 특정돼 일반 삽화로 쓸 수 없다.
#
# 대문자 패턴으로 사람 이름을 잡으려 했으나 실패했다. 영어 제목은
# 일반명사도 대문자로 시작해서 'Taekwondo Class at National Stadium'
# 이 '이름 + at' 구조로 잡혔다. 가장 쓸모 있는 사진이 걸러졌다.
# 고유명사와 일반명사를 표기로는 구분할 수 없으므로 키워드로만 건다.
#
# 이 필터는 완전하지 않다. 인물 사진이 일부 통과하므로 검수 때
# 확인해야 한다. 다만 놓치는 쪽이 멀쩡한 사진을 버리는 것보다 낫다.
EVENT_WORDS = re.compile(
    r"recibimiento|alcalde|alcaldesa|gala\s*award|award\s*ceremony|"
    r"press\s*conference|persconferentie|interview|reception|"
    r"visit\s*(of|to)\b|aankomst|bestanddeelnr|anefo|"
    r"campeon|campeona|winner|winnares|medalist|medallist|"
    r"president|minister|mayor|governor|portrait\s*of",
    re.IGNORECASE,
)

# 세트 끝 번호. 'Children practicing taekwondo ... 01' ~ '09'
SET_SUFFIX = re.compile(r"[\s_-]+\d{1,3}$")

# 같은 세트에서 이만큼만 받는다.
#
# 240건이 240개 장면이 아니다. 한 행사를 연속 촬영해 01~09 로 올린
# 세트가 많아 그대로 받으면 거의 같은 사진이 쌓인다.
PER_SET = 2


def normalize_set(title: str) -> str:
    """세트 식별자를 만든다. 끝 번호를 떼고 소문자로."""
    return SET_SUFFIX.sub("", title or "").strip().lower()


def screen(item: dict) -> str | None:
    """받지 않을 이유를 낸다. 받아도 되면 None."""
    title = item.get("title") or ""

    if EVENT_WORDS.search(title):
        return "의전·인물 기록"

    w, h = item.get("width") or 0, item.get("height") or 0
    if min(w, h) < MIN_SHORT_SIDE:
        return f"해상도 미달 {w}x{h}"

    if (item.get("filetype") or "").lower() not in {"jpg", "jpeg", "png"}:
        return f"형식 {item.get('filetype')}"

    return None


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


def search(client: httpx.Client, query: str, page: int, page_size: int) -> dict:
    resp = client.get(API, params={
        "q": query,
        "license": LICENSES,
        "page": page,
        "page_size": page_size,
        "mature": "false",
    })
    resp.raise_for_status()
    return resp.json()


def download(client: httpx.Client, item: dict, dest: Path) -> bool:
    """받아서 MAX_SIDE 로 줄여 저장한다.

    Wikimedia 원본은 18MB 를 넘기도 한다. 삽화는 Notion 본문 크기라
    1600px 이면 충분하고, 저장소에 원본을 쌓을 이유가 없다.
    """
    try:
        resp = client.get(item["url"], timeout=120,
                          follow_redirects=True,
                          headers={"User-Agent": "taekwonw-agent/0.3 (stock fetch)"})
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
                img = img.resize((round(img.width * ratio), round(img.height * ratio)),
                                 Image.LANCZOS)
            img.save(dest, "JPEG", quality=88)
    except Exception as exc:
        print(f"      이미지 처리 실패, 삭제: {exc}")
        dest.unlink(missing_ok=True)
        return False

    return True


def fetch_category(client: httpx.Client, category: str, spec: dict,
                   sources: dict, dry_run: bool, show_skip: bool) -> int:
    target = spec["target"]
    dest_dir = PENDING_DIR / category

    got = 0
    seen: set[str] = set()
    set_count: dict[str, int] = defaultdict(int)
    skipped: dict[str, int] = defaultdict(int)

    # 이미 받은 사진에서 세트 카운트를 복원한다.
    #
    # 이것이 없으면 재실행할 때마다 같은 세트에서 PER_SET 만큼 더
    # 가져온다. 세트A 9장 중 2장을 받았다면 다음 실행에서 남은 7장 중
    # 2장을 또 받는 식이라, 몇 번 돌리면 세트 전체가 쌓인다.
    for meta in sources.values():
        if meta.get("source") == "openverse":
            set_count[normalize_set(meta.get("title"))] += 1

    print(f"\n[{category}]  목표 {target}장")

    for query in spec["queries"]:
        if got >= target:
            break
        taken = 0
        page = 1

        while got < target and page <= 3:
            try:
                data = search(client, query, page, page_size=20)
            except httpx.HTTPError as exc:
                print(f"  {query} — 조회 실패 ({exc})")
                break

            results = data.get("results") or []
            if not results:
                break

            for item in results:
                if got >= target:
                    break

                # UUID 를 자르지 않는다. 짧게 만들 이유가 없고, 자르면
                # 서로 다른 사진이 같은 이름을 갖게 될 수 있다.
                name = f"openverse-{item['id']}.jpg"
                if name in sources or name in seen:
                    skipped["중복"] += 1
                    continue

                reason = screen(item)
                if reason:
                    skipped[reason.split()[0]] += 1
                    if show_skip:
                        print(f"      건너뜀 [{reason}] {(item.get('title') or '')[:52]}")
                    continue

                # 세트 상한. 같은 행사 연속 촬영이 몰려 들어오는 것을 막는다.
                key = normalize_set(item.get("title"))
                if set_count[key] >= PER_SET:
                    skipped["세트초과"] += 1
                    continue

                seen.add(name)
                set_count[key] += 1

                if dry_run:
                    taken += 1
                    got += 1
                    continue

                if not download(client, item, dest_dir / name):
                    continue

                sources[name] = {
                    "source": "openverse",
                    "provider": item.get("provider", ""),
                    "openverse_id": item["id"],
                    "title": item.get("title", ""),
                    "creator": item.get("creator", ""),
                    "url": item.get("foreign_landing_url", ""),
                    "license": f"{item.get('license', '')} {item.get('license_version', '')}".strip(),
                    "license_url": item.get("license_url", ""),
                    # CC0 는 표기 의무가 없지만 원문을 남겨둔다. 나중에
                    # 문제 제기가 있을 때 출처를 답할 수 있어야 한다.
                    "attribution": item.get("attribution", ""),
                    "category": category,
                    "query": query,
                    "fetched": datetime.now(KST).isoformat(timespec="seconds"),
                }
                taken += 1
                got += 1
                time.sleep(0.3)

            page += 1
            time.sleep(0.3)

        print(f"  {query:<30} +{taken}")

    if skipped:
        detail = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1]))
        print(f"  → {got}/{target}장  (건너뜀: {detail})")
    else:
        print(f"  → {got}/{target}장")
    return got


def main() -> int:
    parser = argparse.ArgumentParser(description="Openverse CC0 스톡 수집")
    parser.add_argument("--dry-run", action="store_true", help="받지 않고 장수만 본다")
    parser.add_argument("--category", help="한 카테고리만")
    parser.add_argument("--show-skip", action="store_true", help="건너뛴 이유를 출력")
    args = parser.parse_args()

    plan = dict(QUERIES)
    if args.category:
        if args.category not in plan:
            print(f"모르는 카테고리: {args.category}")
            print(f"쓸 수 있는 것: {', '.join(plan)}")
            return 1
        plan = {args.category: plan[args.category]}

    sources = load_sources()
    before = len(sources)
    total = 0

    headers = {"User-Agent": "taekwonw-agent/0.3 (stock fetch)"}
    with httpx.Client(headers=headers, timeout=30) as client:
        try:
            for category, spec in plan.items():
                total += fetch_category(client, category, spec, sources,
                                        args.dry_run, args.show_skip)
        finally:
            # 중간에 끊겨도 받은 것까지는 기록을 남긴다.
            if not args.dry_run and len(sources) > before:
                save_sources(sources)

    if args.dry_run:
        print(f"\n미리보기: {total}장을 받게 됩니다.")
        print("--show-skip 을 붙이면 어떤 사진이 왜 걸러졌는지 볼 수 있습니다.")
        return 0

    print(f"\n{total}장을 받아 {PENDING_DIR} 에 넣었습니다.")
    print("\n제목에 taekwondo 가 명시된 것만 받으므로 종목 오염은 적습니다.")
    print("대신 기록 사진 특유의 구도(멀리서 찍은 단체샷, 흐릿한 조명)를")
    print("확인하세요. 삽화로 쓰기 어려운 것이 섞여 있습니다.")
    print("\n다음:")
    print("    python scripts/verify_stock.py init --dir data/stock_pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
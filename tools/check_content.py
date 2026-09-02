"""Content DB 진단 — publish 가 왜 행을 못 찾는지 본다.

뉴스 DB 와 Content DB 는 '상태' 속성을 각각 따로 갖는다. publish 는
Content 쪽만 본다. 보드(뉴스 DB)에서 상태를 바꿔도 publish 대상은
바뀌지 않는다.

눈으로는 '초안대기' 와 '초안 대기' 가 구분되지 않는다. 그래서 옵션
이름과 행 값을 문자 단위로 찍어 비교한다.

사용법
-----
    uv run python -m tools.check_content
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logger import setup  # noqa: E402
from tools.notion_store import _plain_text, _request  # noqa: E402
from tools.notion_content_store import (  # noqa: E402
    PROP_STATUS,
    STATUS_HOLD,
    STATUS_PENDING,
    STATUS_WRITTEN,
    resolve_content_data_source_id,
)


def show(label: str, text: str) -> str:
    """보이는 글자와 코드포인트를 함께 보여준다.

    공백·전각문자·제로폭문자는 눈으로 구분되지 않는다. 하나라도 섞이면
    Notion 필터의 equals 가 어긋나 조회 결과가 0건이 된다.
    """
    codes = " ".join(f"U+{ord(c):04X}" for c in text)
    return f"{label} {text!r}  [{codes}]"


def main() -> int:
    setup()
    ds = resolve_content_data_source_id()
    print("=" * 72)
    print(f"Content data source: {ds}")
    print("=" * 72)

    # ── 1) 코드가 찾는 값 ────────────────────────────
    print("\n[코드가 찾는 값]")
    print("  " + show("STATUS_PENDING =", STATUS_PENDING))

    # ── 2) Notion 에 실제로 있는 옵션 ────────────────
    meta = _request("GET", f"/data_sources/{ds}")
    prop = meta.get("properties", {}).get(PROP_STATUS)

    print(f"\n[Notion '{PROP_STATUS}' 속성]")
    if not prop:
        print(f"  !! '{PROP_STATUS}' 속성이 없습니다.")
        print("     Content DB 의 상태 열 이름이 바뀌었는지 확인하세요.")
        return 1

    ptype = prop.get("type")
    print(f"  타입: {ptype}")
    if ptype != "select":
        print("  !! select 가 아닙니다. 코드는 select 로 조회합니다.")
        return 1

    options = prop.get("select", {}).get("options", [])
    if not options:
        print("  !! 옵션이 하나도 없습니다.")
    matched = False
    for o in options:
        name = o.get("name", "")
        hit = "  <-- 일치" if name == STATUS_PENDING else ""
        matched = matched or bool(hit)
        print("  " + show("-", name) + hit)

    if not matched:
        print(f"\n  !! '{STATUS_PENDING}' 과 정확히 같은 옵션이 없습니다.")
        print("     비슷해 보이는 옵션이 있다면 공백이나 전각문자가 섞인 것입니다.")
        print("     Notion 에서 그 옵션의 '이름을 바꿔' 주세요 (새로 만들지 말 것).")

    # ── 3) 행별 실제 값 ─────────────────────────────
    print("\n[Content 행 목록]")
    body: dict = {"page_size": 100}
    rows: list[tuple[str, str]] = []
    cursor = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        data = _request("POST", f"/data_sources/{ds}/query", json=body)
        for page in data.get("results", []):
            props = page.get("properties", {})
            title = ""
            for p in props.values():
                if p.get("type") == "title":
                    title = _plain_text(p)
                    break
            sel = (props.get(PROP_STATUS) or {}).get("select") or {}
            rows.append((sel.get("name", "") or "(비어 있음)", title))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    if not rows:
        print("  행이 하나도 없습니다. confirm 이 Content 를 만들지 않았습니다.")
        return 1

    counts: dict[str, int] = {}
    for status, title in rows:
        counts[status] = counts.get(status, 0) + 1
        mark = "OK " if status == STATUS_PENDING else "   "
        print(f"  {mark} [{status}] {title[:50]}")

    # ── 4) 요약 ─────────────────────────────────────
    print("\n[상태별 집계]")
    for status in sorted(counts):
        print("  " + show(f"{counts[status]:>3}건", status))

    hit = counts.get(STATUS_PENDING, 0)
    print(f"\npublish 대상: {hit}건")
    if hit:
        print("정상입니다. `run.py publish` 가 이 행들을 집어 갑니다.")
    else:
        print(f"publish 는 '{STATUS_PENDING}' 만 조회하므로 아무것도 하지 않습니다.")
        print(f"(다른 상태: '{STATUS_WRITTEN}' = 이미 작성됨, '{STATUS_HOLD}' = 제외됨)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
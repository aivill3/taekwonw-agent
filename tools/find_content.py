"""Content DB 진단 — 데이터 소스별로 특정 날짜 이후 만들어진 페이지를 찾는다.

읽기 전용이다. 아무것도 고치지 않는다.

    uv run python -m tools.find_content              # 오늘(KST) 이후
    uv run python -m tools.find_content 2026-09-23   # 지정한 날짜 이후

왜 필요한가 (2026-09-23)
-----------------------
로그에는 'Content 생성 · Notion 저장 완료 3/3' 이 찍혔는데 보드에는 글이
없었다. resolve_content_data_source_id() 는 DB 의 첫 번째 데이터 소스에만
쓴다. DB 에 데이터 소스가 여럿이면, 사람이 보는 표와 코드가 쓰는 곳이
갈라질 수 있다. 어느 소스에 무엇이 있는지 직접 확인한다.
"""
import sys
from datetime import datetime

from config.settings import KST, NOTION_CONTENT_DATABASE_ID
from tools.notion_store import _request


def _title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop["title"]) or "(제목 없음)"
    return "(제목 속성 없음)"


def _select(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name) or {}
    sel = prop.get("select") or {}
    return sel.get("name", "")


def main() -> None:
    since = sys.argv[1] if len(sys.argv) > 1 else datetime.now(KST).strftime("%Y-%m-%d")

    db = _request("GET", f"/databases/{NOTION_CONTENT_DATABASE_ID}")
    sources = db.get("data_sources", [])
    db_title = "".join(t.get("plain_text", "") for t in db.get("title", []))
    print(f"DB: {db_title or '(제목 없음)'}  ({NOTION_CONTENT_DATABASE_ID})")
    print(f"데이터 소스 {len(sources)}개 — 코드는 1번에 쓴다\n")

    for i, src in enumerate(sources, 1):
        ds_id = src["id"]
        print(f"[{i}] {src.get('name') or '(이름 없음)'}  ({ds_id})")

        # 검색키워드 옵션 — 이번 실행에서 '대회' 가 추가됐는지로 쓴 곳을 가린다
        schema = _request("GET", f"/data_sources/{ds_id}")
        kw = schema.get("properties", {}).get("검색키워드", {})
        options = [o["name"] for o in kw.get("select", {}).get("options", [])]
        print(f"    검색키워드 옵션: {', '.join(options) or '(속성 없음)'}")

        res = _request(
            "POST",
            f"/data_sources/{ds_id}/query",
            json={
                "filter": {
                    "timestamp": "created_time",
                    "created_time": {"on_or_after": since},
                },
                "sorts": [{"timestamp": "created_time", "direction": "descending"}],
                "page_size": 20,
            },
        )
        pages = res.get("results", [])
        print(f"    {since} 이후 생성 {len(pages)}편")
        for p in pages:
            print(
                f"      - {p['created_time']}  [{_select(p, '검색키워드') or '-'}] "
                f"{_title(p)}\n        {p.get('url', '')}"
            )
        print()


if __name__ == "__main__":
    main()
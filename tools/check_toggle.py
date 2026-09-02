"""원문 토글 구조 확인."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.article_models import Article
from tools.notion_store import (
    create_page, resolve_data_source_id, fetch_page_body, find_origin_toggle_id,
)

a = Article(title="[테스트] 토글 확인용", url="https://example.com/toggle-test-1")
a.body_clean = "첫 문단입니다.\n\n둘째 문단입니다.\n셋째 문단입니다."

ds = resolve_data_source_id()
pid = create_page(ds, a)
print("생성:", pid)

print("토글 id:", find_origin_toggle_id(pid))
body = fetch_page_body(pid)
print("본문 길이:", len(body))
print("본문:", repr(body))
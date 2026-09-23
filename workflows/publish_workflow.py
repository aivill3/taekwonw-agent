"""블로그 초안 작성 — 자동 파이프라인의 뒷단.

collect_workflow 가 키워드별로 묶은 글 목록을 publish_bundles() 에 넘긴다.

흐름:
  초안 생성(LLM #2) → Content 행 생성(제목·본문 확정 상태로) → 품질 측정
  → 초안 본문 붙이기 → Slack 알림 → 소제목별 삽화 → 로컬 저장

뉴스 DB 를 쓰지 않는다 (2026-09-17)
---------------------------------
예전에는 뉴스 DB '초안요청' 카드를 묶음ID 별로 모아 되살렸다(run, load_bundles).
승인 게이트와 뉴스 보드를 없애면서 그 경로를 지웠다. 원문 기사는 Content
페이지 안의 토글에 들어가고, 검색키워드·검색순위·수집회차가 속성으로 붙는다.

저장 순서 — 삽화보다 Notion 이 먼저
--------------------------------
초안은 write_all 이 돌려준 메모리 안에만 있다. 삽화를 먼저 만들다 프로세스가
끊기면 초안과 거기 쓴 LLM 할당량이 함께 사라진다. 그래서 Content 에 먼저
넣고, 삽화는 뒤에 붙인다. 삽화가 실패해도 `run.py images` 로 채울 수 있다.

저장 위치:
  - Notion 본문: Content 페이지. 원문은 토글로 접혀 있어 초안이 먼저 보인다.
  - 로컬 파일: 발행 시 복사해 쓸 원본 (평문 + png)

  '블로그원고' 속성에는 저장하지 않는다. 속성은 2,000자 상한이라 긴 초안이
  잘리고, 같은 내용이 속성과 본문에 이중으로 남아 어느 쪽이 최신인지 흐려진다.

재작성 방지:
  저장까지 끝난 글의 기사는 collect 가 처리 이력(state)에 남긴다.
  실패한 글의 기사는 남기지 않아 다음 실행에서 다시 후보가 된다.
"""
import re
from datetime import datetime

from config.settings import DRAFT_DIR, IMAGE_SOURCE, KST
from core.logger import get_logger
from tools.slack_notifier import notify_published
from tools.notion_store import append_illustrated_draft, insert_images_at_slots
from tools.notion_content_store import (
    STATUS_WRITTEN,
    create_content,
    ensure_content_schema,
    extract_draft_title,
    resolve_content_data_source_id,
    save_content_quality,
)
from agents.quality.quality_gate import (
    append_metrics,
    detail_reports,
    format_summary,
    measure_all,
    notion_summary,
)
from agents.drafting.schema_draft_agent import write_all

log = get_logger(__name__)

_RE_SLUG = re.compile(r"[^0-9A-Za-z가-힣]+")


def make_slug(title: str) -> str:
    """파일명용 슬러그. 한글은 유지하고 기호만 밑줄로 바꾼다."""
    slug = _RE_SLUG.sub("_", title).strip("_")
    return slug[:40] or "draft"


RE_SUBHEAD = re.compile(r"/\*\s*소제목\s*\*/")
RE_IMAGE_SLOT = re.compile(r"^\d{1,2}$")
# '1. 첫 번째는' 같은 서두는 검색어로 쓸모가 없으므로 떼어낸다.
RE_CHAPTER_LEAD = re.compile(
    r"^\d+\.\s*(?:첫|두|세|네|다섯|여섯)\s*번째는\s*|^\d+\.\s*"
)


def section_headings(draft: str) -> list[str]:
    """삽화 검색어로 쓸 소제목 목록.

    `/* 소제목 */` 이 붙은 줄부터 빈 줄 전까지가 한 챕터의 서두다.
    마커 줄만 읽으면 '1. 첫 번째는' 처럼 내용이 없는 문구가 나오므로
    (실제 주제어는 다음 줄에 있다) 덩어리를 합쳐서 쓴다.
    """
    out: list[str] = []
    lines = draft.split("\n")
    for i, line in enumerate(lines):
        if not RE_SUBHEAD.search(line):
            continue
        parts = [RE_SUBHEAD.sub("", line).strip()]
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                break
            parts.append(nxt.strip())
        text = " ".join(p for p in parts if p)
        text = RE_CHAPTER_LEAD.sub("", text).strip()
        text = text.removesuffix("이야기예요.").removesuffix("이야기에요.").strip()
        if text:
            out.append(text)
    return out


def count_image_slots(draft: str) -> int:
    """숫자만 있는 줄 = 이미지 자리. 몇 개인지 센다."""
    return sum(1 for l in draft.split("\n") if RE_IMAGE_SLOT.match(l.strip()))


def collect_images(
    headings: list[str],
    slug: str,
    text_model: str,
    *,
    title: str = "",
    body: str = "",
) -> dict[int, dict]:
    """소제목별 삽화를 생성한다.

    임포트를 함수 안에서 하는 이유: IMAGE_SOURCE=none 으로 끄고 쓸 때
    이미지 모듈의 의존성을 끌어오지 않기 위함이다.

    삽화 확보에 실패해도 예외를 올리지 않는다. 이미지가 없어도 글은 성립하고,
    무료 티어에서는 이미지 모델 할당량이 없어 429가 흔하다.

    title/body 는 planned 경로에서만 쓴다. 기사 전체를 읽어야 소제목마다
    다른 장면을 계획할 수 있기 때문이다.
    """
    if IMAGE_SOURCE == "none" or not headings:
        return {}
    if IMAGE_SOURCE == "hybrid":
        # planned 와 같은 분석·계획을 쓰되, 생성 전에 스톡 라이브러리를 먼저 본다.
        # 맞는 사진이 있으면 그것을 쓰고 없는 자리만 생성한다.
        from agents.illustration.hybrid_image_agent import generate_for_sections

        return generate_for_sections(
            headings, slug, text_model, title=title, body=body
        )
    if IMAGE_SOURCE == "planned":
        # v3 1차: 기사 분석 → Visual Plan → 생성 → 표시.
        # 소제목 키워드 매칭 대신 기사 맥락으로 장면을 계획한다.
        from agents.illustration.planned_image_agent import generate_for_sections

        return generate_for_sections(
            headings, slug, text_model, title=title, body=body
        )
    if IMAGE_SOURCE == "local":
        # 로컬 diffusers. 할당량은 없지만 CPU에서는 장당 수십 초~수 분.
        from agents.illustration.local_image_agent import generate_for_sections

        return generate_for_sections(headings, slug)
    if IMAGE_SOURCE == "gemini":
        from agents.illustration.generated_image_agent import generate_for_sections

        return generate_for_sections(headings, slug, text_model)
    log.warning(
        f"알 수 없는 IMAGE_SOURCE: {IMAGE_SOURCE} — "
        f"'hybrid' | 'planned' | 'local' | 'gemini' | 'none'"
    )
    return {}


def save_local(draft: str, slug: str, images: dict) -> None:
    """원고를 로컬에 저장한다. 숫자 자리에 이미지 참조와 캡션을 넣는다.

    번호 줄을 이미지로 바꾸지 않고 그 아래에 참조를 덧붙인다.
    자리 번호가 남아 있어야 이미지를 못 구한 칸을 사람이 찾아 채울 수 있다.
    """
    lines: list[str] = []
    slot_idx = -1
    for line in draft.split("\n"):
        lines.append(line)
        if not RE_IMAGE_SLOT.match(line.strip()):
            continue
        slot_idx += 1
        img = images.get(slot_idx)
        if not img:
            continue
        # 이미지와 원고를 같은 폴더 기준 상대경로로 참조
        lines.append("")
        lines.append(f"![{slot_idx + 1}](../images/{img['path'].name})")
        caption = img.get("caption", "")
        if caption:
            lines.append(f"*{caption}*")
    path = DRAFT_DIR / f"{datetime.now(KST):%Y%m%d}_{slug}.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"로컬 저장: {path.name}")


def publish_bundles(
    bundles: list[dict],
    *,
    dry_run: bool = False,
    with_images: bool = True,
    with_quality: bool = True,
) -> list[dict]:
    """묶음 목록으로 초안을 쓰고 Content DB 에 저장한다.

    반환: Content 저장까지 끝난 항목. collect 가 처리 이력을 남길 때 쓴다.

    bundles 항목: bundle_id, title, url, subtopics, body, brief, kind, reason,
    search_keyword, search_ranks(brief.articles 순서), run_at
    """
    for it in bundles:
        brief = it["brief"]
        lo, hi, ch = brief.target_length()
        log.info(
            f"  [{it['bundle_id']}] {it['title'][:40]} "
            f"(기사 {len(brief)}건 · 소주제 {len(it['subtopics'])}개 "
            f"· 원문 {brief.source_chars:,}자 -> 목표 {lo:,}~{hi:,}자 · 챕터 {ch}개)"
        )

    # 2) 블로그 초안 생성 (LLM #2)
    #    item["brief"] 가 있으면 write_all 이 묶음으로 처리한다.
    written = write_all(bundles)
    if not written:
        log.warning("생성된 초안이 없습니다.")
        return []

    # 5) 슬러그 준비. 삽화는 아직 만들지 않는다 — 6단계에서 초안을 먼저
    #    Notion에 넣은 뒤에 붙인다. (아래 7단계 주석 참고)
    for it in written:
        # 첫 줄이 제목이다 (마크다운 '# ' 없음)
        it["slug"] = make_slug(it["markdown"].split("\n", 1)[0].strip())
        it["images"] = {}

    if dry_run:
        for it in written:
            log.info(f"[dry-run] {it['title'][:40]}")
            print("\n" + "=" * 70)
            print(it["markdown"])
            print("=" * 70 + "\n")
        if with_quality:
            m = measure_all(written)
            if m:
                print("\n" + detail_reports(m) + "\n")
        log.info("[dry-run] Notion에 저장하지 않았습니다.")
        return []

    # 6) Notion 저장 — 삽화보다 '먼저'.
    #
    #    초안은 write_all 이 돌려준 메모리 안에만 있다. 삽화 생성을 먼저 하면
    #    CPU 로컬 생성이 편당 10~20분씩 걸리는 동안 초안이 디스크에도 Notion에도
    #    없는 상태가 이어지고, 그때 프로세스가 끊기면 초안과 거기 쓴 LLM
    #    할당량이 함께 사라진다. 무료 티어에서는 그날 다시 못 쓸 수도 있다.
    #    (실측 2026-08-14: 3/3건 작성 성공 후 삽화 단계에서 멈춰 Notion 미반영)
    #
    #    삽화는 없어도 글은 글이고, 나중에 `py run.py images` 로 채울 수 있다.
    content_ds = resolve_content_data_source_id()
    try:
        ensure_content_schema(content_ds)
    except Exception as e:
        log.warning(f"Content 스키마 보정 실패(수동 추가가 필요할 수 있음): {e}")

    # 6a) Content 행 생성 — 제목과 본문이 확정된 지금 만든다.
    #     초안 첫 줄이 제목이라는 draft_prompt 출력 형식을 따른다.
    for it in written:
        it["saved"] = False
        real_title = extract_draft_title(it["markdown"])
        try:
            it["page_id"] = create_content(
                content_ds,
                it["brief"],
                kind=it.get("kind", "topic"),
                reason=it.get("reason", ""),
                news_page_ids=[],
                title=real_title,
                status=STATUS_WRITTEN,
                search_keyword=it.get("search_keyword", ""),
                search_ranks=it.get("search_ranks"),
                run_at=it.get("run_at", ""),
            ) or ""
        except Exception as e:
            # 원고는 이미 만들어졌지만 저장에 실패했다. 로컬 파일로는
            # 남으므로(8단계) 사람이 살릴 수 있다.
            log.warning(f"Content 생성 실패 ({it['title'][:40]}): {e}")
            it["page_id"] = ""
            continue
        log.info(f"Content 생성: {real_title[:50]}")

    # 6b) 품질 측정 — 발행을 막지 않는다.
    #     현행 마크다운 초안의 실제 지표와 네이버 형식 전환 거리를 누적한다.
    #     삽화 확보 전에 재는 이유: 이미지 삽입 후에는 원고 구조가 바뀌어
    #     문단·줄 통계가 LLM 출력 그대로가 아니게 된다.
    #     Content 생성 뒤에 재는 이유: page_id 가 있어야 '형태소' 속성에 남길
    #     수 있는데, 그 id 는 방금 만들어졌다.
    metrics = []
    reports: dict[str, str] = {}   # page_id -> 노션 '형태소' 속성에 넣을 요약
    if with_quality:
        metrics = measure_all(written)
        if metrics:
            log.info("\n" + format_summary(metrics))
            append_metrics(metrics)
            reports = {m.page_id: notion_summary(m) for m in metrics if m.page_id}

    # 6c) 초안 본문을 붙인다
    saved = 0
    for it in written:
        if not it.get("page_id"):
            continue
        try:
            # 원문 기사는 create_content 가 토글로 넣어 두었으므로
            # 페이지를 열면 초안이 먼저 보인다.
            append_illustrated_draft(it["page_id"], it["markdown"], {})

            # 품질 측정 요약을 '형태소' 속성에 남긴다 (측정을 껐으면 건너뛴다)
            summary = reports.get(it["page_id"])
            if summary:
                save_content_quality(it["page_id"], summary, it.get("model", ""))

            it["saved"] = True
            saved += 1
            log.info(f"Notion 저장 완료: {it['title'][:40]}")
        except Exception as e:
            # Content 행은 생겼는데 본문이 비었다. 처리 이력에 남지 않으므로
            # 다음 실행에서 같은 기사로 새 행이 생긴다 — 빈 행은 지워야 한다.
            log.warning(
                f"초안 본문 저장 실패 ({it['title'][:40]}): {e} "
                f"— 빈 Content 행이 남았습니다. Notion 에서 지워 주세요"
            )

    log.info(f"초안 저장 완료 {saved}/{len(written)}건")

    # Slack 알림 — 삽화를 기다리지 않고 원고 확인으로 먼저 유도한다
    if saved:
        notify_published(written)

    # 7) 삽화 생성 후 자리에 끼워 넣기. 여기서 실패해도 초안은 이미 안전하다.
    if with_images:
        for it in written:
            if not it.get("saved"):
                continue
            try:
                it["images"] = collect_images(
                    section_headings(it["markdown"]),
                    it["slug"],
                    it.get("model", ""),
                    title=it.get("title", ""),
                    # planned 경로는 기사 원문을 읽어 장면을 계획한다.
                    # 묶음 글이면 대표 기사 본문이 들어온다.
                    body=it.get("body", ""),
                )
                inserted = insert_images_at_slots(it["page_id"], it["images"])
                log.info(f"삽화 {inserted}장 삽입: {it['title'][:40]}")
            except Exception as e:
                log.warning(
                    f"삽화 실패({it['title'][:30]}): {e} "
                    f"— 초안은 저장됨, `run.py images` 로 나중에 채울 수 있습니다"
                )

    # 8) 로컬 저장 (마크다운 + 이미지 참조)
    for it in written:
        save_local(it["markdown"], it["slug"], it["images"])


    return [it for it in written if it.get("saved")]
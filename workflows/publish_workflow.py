"""블로그 초안 작성 파이프라인 (확정 이후 단계).

흐름:
  뉴스 DB '초안요청' 조회 → 묶음ID 별로 모아 DraftBrief 재구성
  → 블로그 초안 생성(LLM #2) → Content 행 생성(제목·본문 확정 상태로)
  → 소제목별 삽화 확보 → 로컬 저장 → News 를 '작성완료'로

Content 를 여기서 만든다
----------------------
예전에는 confirm 이 Content 를 미리 만들었다. 그 시점에는 제목도 본문도
없어서 '⏳ …' 임시 제목을 달았다가 여기서 덮어써야 했고, 확정을 취소하려면
뉴스 DB 와 Content DB 두 곳을 손봐야 했다.

이제 초안이 완성된 뒤에 만든다. Content DB 는 '써진 글의 보관함'이 되고,
확정 취소는 보드에서 카드를 '선정됨'으로 되돌리는 것으로 끝난다.

묶음을 어떻게 되살리나
--------------------
confirm 이 각 기사에 '20260901-01' 같은 묶음ID 를 붙여 두었다. 같은
묶음ID 를 가진 기사들이 글 한 편이다. 소주제·본문은 뉴스 페이지에서
그대로 읽는다.

  묶음 4건  → 기사 1건이 챕터 1개 (소주제 1개씩)
  단독 1건  → 그 기사가 챕터 4개 (소주제 4개)

한 번에 몇 편을 돌릴지
--------------------
Gemini 2.0 Flash 는 RPD 20 이라 재시도까지 감안하면 하루 10편이 실질
상한이다. --limit 로 나눠 돌린다. 지정하지 않으면 '초안요청' 전부를
처리하므로, 확정분이 쌓여 있으면 먼저 확인하는 편이 안전하다.

특정 묶음만 돌리려면 --bundle-id 를 쓴다. --limit 은 앞에서 N편을 자를
뿐이라 '이 편을 지금' 이 안 된다.

    run.py publish --limit 3                  앞에서 3편
    run.py publish --bundle-id 20260902-04    그 편만

저장 위치:
  - Notion 본문: Content 페이지. 원문은 토글로 접혀 있어 초안이 먼저 보인다.
  - 로컬 파일: 발행 시 복사해 쓸 원본 (평문 + png)

  '블로그원고' 속성에는 저장하지 않는다. 속성은 2,000자 상한이라 긴 초안이
  잘리고, 같은 내용이 속성과 본문에 이중으로 남아 어느 쪽이 최신인지 흐려진다.

재작성 방지:
  뉴스 상태가 '초안요청'인 것만 조회하고, 작성 후 '작성완료'로 넘긴다.

슬롯 반납:
  초안이 나간 뒤에 '묶음' 슬롯을 비운다 (finish_article). 확정 시점에
  비우지 않는 것은, 초안을 기다리는 동안 카드가 보드에 남아 있어야
  무엇이 진행 중인지 보이기 때문이다.
"""
import re
from datetime import datetime

from config.settings import DRAFT_DIR, IMAGE_SOURCE, KST
from core.logger import get_logger, setup
from tools.slack_notifier import notify_failure, notify_published
from tools.notion_store import (
    STATUS_DEFAULT,
    STATUS_REQUESTED_DRAFT,
    append_illustrated_draft,
    fetch_by_status,
    finish_article,
    insert_images_at_slots,
    update_status,
    resolve_data_source_id,
)
from tools.notion_content_store import (
    STATUS_WRITTEN,
    create_content,
    ensure_content_schema,
    extract_draft_title,
    load_source,
    resolve_content_data_source_id,
    save_content_quality,
)
from agents.drafting.article_grouper import CHAPTERS_PER_POST
from agents.drafting.draft_prompt import DraftBrief
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


def build_brief(members: list[dict], solo: bool) -> DraftBrief:
    """확정된 배치로 DraftBrief 를 만든다.

    묶음이면 기사 1건이 챕터 1개를 맡으므로 소주제를 1개로 줄인다.
    (article_grouper._as_one_chapter 와 같은 규칙)

    confirm 에 있던 것을 옮겨 왔다. 목표분량·기사수는 실제 배치로
    계산해야 하는데, 그 배치를 아는 시점이 이제 여기이기 때문이다.
    """
    articles = []
    for a in members:
        if not solo and len(a.subtopics) > 1:
            a.subtopics = a.subtopics[:1]
        elif solo and len(a.subtopics) > CHAPTERS_PER_POST:
            a.subtopics = a.subtopics[:CHAPTERS_PER_POST]
        articles.append(a)

    head = articles[0]
    return DraftBrief(
        articles=articles,
        title_hint=head.title,
        matched_keyword=next((a.matched_keyword for a in articles if a.matched_keyword), ""),
    )


def _revert(item: dict) -> None:
    """실패한 묶음의 뉴스 상태를 '선정됨' 으로 되돌린다.

    '초안요청' 으로 남겨 두면 30분마다 도는 폴링이 같은 묶음을 계속
    재시도한다. Gemini RPD 20 이라 몇 번 만에 그날 할당량이 사라진다.

    되돌리면 카드가 보드 제자리로 돌아온다(슬롯은 그대로다). 사람이
    확인하고 버튼을 다시 누르면 재시도된다.
    """
    for pid in item.get("pages", []):
        try:
            update_status(pid, STATUS_DEFAULT)
        except Exception as e:
            log.error(f"상태 되돌리기 실패 ({pid[:8]}): {e}")
    log.info(f"'{STATUS_DEFAULT}' 로 되돌렸습니다 — 버튼을 다시 누르면 재시도합니다")


def load_bundles(
    news_ds: str,
    limit: int | None = None,
    bundle_id: str | None = None,
    status: str = STATUS_REQUESTED_DRAFT,
) -> list[dict]:
    """뉴스 DB '초안요청' 을 묶음ID 별로 모아 글 단위로 돌려준다.

    묶음ID 가 비어 있는 기사는 건너뛴다. confirm 을 거치지 않고 사람이
    상태만 '초안요청'으로 바꾼 경우인데, 어느 글에 속하는지 알 수 없다.

    bundle_id 를 주면 그 묶음 하나만 돌려준다
    ---------------------------------------
    limit 은 개수만 자른다. 묶음ID 순으로 정렬해 앞에서 자르므로 어느 편이
    돌아갈지는 코드가 정한다. '04번만 지금 돌려라' 가 안 된다.

    Notion 버튼은 행에 붙는다. 사용자가 어떤 카드의 버튼을 눌렀는지가 곧
    '그 묶음'이므로, 러너가 그것을 명령으로 옮기려면 지목 수단이 있어야 한다.
    """
    items = fetch_by_status(news_ds, status)
    if not items:
        return []

    groups: dict[str, list[dict]] = {}
    orphans: list[dict] = []
    for it in items:
        bid = it.get("bundle_id", "")
        if bid:
            groups.setdefault(bid, []).append(it)
        else:
            orphans.append(it)

    # 묶음을 지목한 실행에서는 관계없는 기사를 일일이 알리지 않는다.
    # 러너가 버튼 한 번에 이 워크플로를 부르므로 로그가 그만큼 길어진다.
    if orphans and not bundle_id:
        log.warning(f"묶음ID 가 없는 '{status}' 기사 {len(orphans)}건을 건너뜁니다.")
        for it in orphans:
            log.warning(f"    - {it['title'][:52]}")
        log.warning("보드에서 '선정됨'으로 되돌린 뒤 다시 확정하세요.")

    if bundle_id:
        if bundle_id not in groups:
            known = ", ".join(sorted(groups)) or "(없음)"
            log.warning(f"묶음ID '{bundle_id}' 를 찾지 못했습니다.")
            log.warning(f"'{status}' 상태의 묶음: {known}")
            return []
        groups = {bundle_id: groups[bundle_id]}
        log.info(f"묶음 '{bundle_id}' 만 처리합니다 ({len(groups[bundle_id])}건)")

    bundles: list[dict] = []
    for bid in sorted(groups):
        members = groups[bid]
        # 원문 본문과 소주제는 뉴스 페이지에서 읽는다.
        # 사람이 Notion 에서 소주제를 고쳤다면 그 값이 그대로 들어온다.
        articles = [a for a in (load_source(m["page_id"]) for m in members) if a]
        if not articles:
            log.warning(f"[{bid}] 원문을 하나도 읽지 못해 건너뜁니다.")
            continue
        if len(articles) != len(members):
            log.warning(
                f"[{bid}] 기사 {len(members)}건 중 {len(articles)}건만 읽혔습니다. "
                f"분량이 목표에 못 미칠 수 있습니다."
            )

        solo = len(members) == 1
        brief = build_brief(articles, solo)
        bundles.append({
            "bundle_id": bid,
            "page_id": "",                       # Content 는 초안 작성 후에 만든다
            "pages": [m["page_id"] for m in members],
            "title": brief.title_hint,
            "url": next((a.url for a in articles if a.url), ""),
            "subtopics": [t for a in articles for t in a.subtopics],
            "body": articles[0].content,         # planned 삽화가 참고할 대표 본문
            "brief": brief,
            "kind": "topic" if solo else "daily",
            "reason": f"[{bid}] {'단독' if solo else '묶음'} {len(articles)}건",
        })

        if limit and len(bundles) >= limit:
            log.info(f"--limit {limit} 에 걸려 나머지 묶음은 다음 실행으로 미룹니다.")
            break

    return bundles


def run(
    *,
    dry_run: bool = False,
    with_images: bool = True,
    with_quality: bool = True,
    limit: int | None = None,
    bundle_id: str | None = None,
    no_bundle: bool = False,   # 하위호환용. 묶기는 content_workflow 로 옮겨 무시된다.
) -> None:
    setup()
    log.info("=" * 50)
    log.info("블로그 초안 작성 시작" + (" [DRY-RUN]" if dry_run else ""))
    log.info("=" * 50)

    try:
        news_ds = resolve_data_source_id()

        # 1) 뉴스 DB '초안요청' 을 묶음ID 별로 모은다.
        #    한 묶음이 곧 글 한 편이다. 묶기는 content 가, 확정은 confirm 이
        #    이미 끝냈고, 여기서는 배치를 그대로 복원할 뿐이다.
        # 사람이 [초안 작성] 을 눌러 '초안요청' 이 된 것만 본다.
        src_status = STATUS_REQUESTED_DRAFT

        bundles = load_bundles(news_ds, limit, bundle_id, src_status)
        if not bundles:
            # 폴링이 30분마다 돈다. 요청이 없는 것이 정상이므로
            # Slack 알림을 보내지 않는다.
            if bundle_id:
                log.info(f"묶음 '{bundle_id}' 로 작성할 것이 없습니다.")
            else:
                log.info(f"'{src_status}' 묶음이 없습니다. 할 일이 없습니다.")
            return

        log.info(f"{src_status} 묶음 {len(bundles)}편")
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
            return

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
            return

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
            ensure_content_schema(content_ds, news_ds)
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
                    news_page_ids=it.get("pages", []),
                    title=real_title,
                    status=STATUS_WRITTEN,
                ) or ""
            except Exception as e:
                # 원고는 이미 만들어졌지만 저장에 실패했다. 로컬 파일로는
                # 남으므로(8단계) 사람이 살릴 수 있다.
                log.warning(f"Content 생성 실패 ({it['title'][:40]}): {e}")
                it["page_id"] = ""
                _revert(it)
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

        # 6c) 초안 본문을 붙이고 뉴스 상태를 넘긴다
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

                # 재료로 쓰인 News 를 모두 '작성완료'로 넘기고 슬롯을 반납한다.
                # '초안요청'으로 남으면 다음 실행에서 같은 글을 또 쓰고,
                # 슬롯이 남으면 다음 회차 배정분이 같은 칸에 얹힌다.
                for pid in it.get("pages", []):
                    finish_article(pid)

                it["saved"] = True
                saved += 1
                log.info(f"Notion 저장 완료: {it['title'][:40]}")
            except Exception as e:
                # 본문 저장은 됐는데 상태 변경이 실패하면 다음 실행에서 중복 작성될 수 있다.
                log.warning(f"저장 실패 ({it['title'][:40]}): {e}")
                _revert(it)

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

        log.info("파이프라인 종료")
    except Exception as e:
        log.exception("초안 작성 파이프라인 중단급 오류 발생")
        if not dry_run:
            notify_failure("블로그 초안 작성", e)
        raise
"""Slack 알림 (Incoming Webhook).

보내는 시점:
  1. 수집 완료 — 선정 기사 목록 (제목·URL·소주제·Notion 링크)
  2. 초안 작성 완료 — 작성된 글 목록
  3. 실행 실패 — 오류 요약

설계 원칙:
  - 알림 실패가 파이프라인을 멈추면 안 된다. 모든 예외를 삼키고 로그만 남긴다.
  - Notion 페이지 링크를 반드시 넣는다. 알림에서 바로 승인 체크로 갈 수 있어야
    승인 게이트의 마찰이 줄어든다.
  - 소주제는 판단 재료이므로 본문에 넣되, 상세 확인·수정은 Notion으로 유도한다.
"""
import json
import traceback

import requests

from config.settings import SLACK_WEBHOOK_URL
from core.logger import get_logger, logfile

log = get_logger(__name__)

TIMEOUT = 10
MAX_BLOCKS = 45  # Slack 메시지당 블록 상한(50)에 여유를 둔 값


def notion_url(page_id: str) -> str:
    """page_id → Notion 페이지 URL (하이픈 제거 형식)."""
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _post(payload: dict) -> bool:
    """Webhook 전송. 실패해도 예외를 밖으로 내보내지 않는다."""
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL 이 없어 알림을 건너뜁니다")
        return False
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning(f"Slack 전송 실패 [{resp.status_code}]: {resp.text[:150]}")
            return False
        return True
    except Exception as e:
        log.warning(f"Slack 전송 오류: {e}")
        return False


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def notify_selected(articles: list, stats: dict | None = None) -> None:
    """수집 완료 알림 — 선정된 기사와 소주제.

    articles: Article 리스트 (page_id, title, url, subtopics, score_norm, report_count)
    stats: {"collected": 66, "selected": 5} 형태의 요약 (선택)
    """
    if not articles:
        return

    header = f"오늘의 태권도 뉴스 {len(articles)}건이 선정됐습니다"
    if stats:
        header += f"  (수집 {stats.get('collected', '?')}건 중)"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "📰 뉴스 선정 완료"}},
        _section(header),
        {"type": "divider"},
    ]

    for i, a in enumerate(articles, 1):
        page_id = getattr(a, "page_id", "")
        score = getattr(a, "score_norm", 0)
        count = getattr(a, "report_count", 1)

        badge = f"`{score}점`"
        if count > 1:
            badge += f" · `{count}개 매체 보도`"

        lines = [f"*{i}. {a.title}*", badge]
        if a.url:
            lines.append(f"<{a.url}|원문 보기>")
        if getattr(a, "subtopics", None):
            lines.append("")
            lines.extend(f"　• {s}" for s in a.subtopics)
        if page_id:
            lines.append(f"\n<{notion_url(page_id)}|👉 Notion에서 승인하기>")

        blocks.append(_section("\n".join(lines)))
        if i < len(articles):
            blocks.append({"type": "divider"})

    _post({"text": header, "blocks": blocks[:MAX_BLOCKS]})
    log.info(f"Slack 알림 전송: 선정 {len(articles)}건")


def notify_published(items: list[dict]) -> None:
    """초안 작성 완료 알림.

    items: [{page_id, title, markdown, model, images}]
    """
    if not items:
        return

    header = f"블로그 초안 {len(items)}건이 작성됐습니다"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "✍️ 초안 작성 완료"}},
        _section(header),
        {"type": "divider"},
    ]

    for i, it in enumerate(items, 1):
        md = it.get("markdown", "")
        title = md.split("\n", 1)[0].removeprefix("# ").strip() or it.get("title", "")
        meta = [f"{len(md)}자"]
        if it.get("images"):
            meta.append(f"삽화 {len(it['images'])}장")
        if it.get("model"):
            meta.append(it["model"])

        lines = [f"*{i}. {title}*", f"`{' · '.join(meta)}`"]
        if it.get("page_id"):
            lines.append(f"<{notion_url(it['page_id'])}|👉 Notion에서 원고 보기>")
        blocks.append(_section("\n".join(lines)))

    _post({"text": header, "blocks": blocks[:MAX_BLOCKS]})
    log.info(f"Slack 알림 전송: 초안 {len(items)}건")


def notify_empty(stage: str, reason: str) -> None:
    """선정/작성 결과가 없을 때. 실행은 됐다는 신호를 남긴다
    (알림이 아예 안 오면 cron 실패와 구분되지 않는다)."""
    text = f"⚪️ *{stage}*: {reason}"
    _post({"text": text, "blocks": [_section(text)]})


def notify_failure(stage: str, error: BaseException) -> None:
    """실행 실패 알림.

    자동화의 가장 큰 위험은 '조용히 멈추는 것'이다.
    cron이 실패했을 때 알 수 있도록 오류 요약을 보낸다.
    """
    tb = "".join(traceback.format_exception_only(type(error), error)).strip()
    text = f"🚨 *{stage} 실패*\n```{tb[:800]}```"
    # 로그 파일명은 단계별로 나뉘므로(collect_*.log 등) 실제 경로를 그대로 알린다.
    path = logfile()
    hint = f"logs/{path.name}" if path else "logs/ 폴더"
    _post({
        "text": f"{stage} 실패: {tb[:150]}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "🚨 파이프라인 실패"}},
            _section(text),
            _section(f"_{hint} 에서 전체 traceback을 확인하세요._"),
        ],
    })
    log.info("Slack 알림 전송: 실패 통지")

def notify_held(items: list[dict], max_days: int = 3) -> None:
    """묶을 짝이 없어 보류된 기사 알림.

    items: [{page_id, title, chars, days}]
      days = 승인된 뒤 지난 일수

    max_days 를 넘긴 건은 알리지 않는다. 매일 같은 기사를 계속 알리면
    알림을 무시하게 되고, 사흘이 지나도 짝이 없으면 그날의 뉴스로서
    가치가 이미 떨어진 것이라 손으로 정리하는 편이 맞다.
    """
    fresh = [it for it in items if it.get("days", 0) <= max_days]
    if not fresh:
        return

    header = f"묶을 짝이 없어 보류된 기사 {len(fresh)}건"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "⏸️ 발행 보류"}},
        _section(
            f"{header}\n"
            f"다른 기사를 함께 승인하면 한 편으로 묶여 발행됩니다. "
            f"{max_days}일이 지나면 알림이 멈춥니다."
        ),
        {"type": "divider"},
    ]

    for i, it in enumerate(fresh, 1):
        meta = [f"{it.get('chars', 0)}자"]
        days = it.get("days", 0)
        meta.append("오늘 승인" if days == 0 else f"{days}일째 대기")
        lines = [f"*{i}. {it.get('title', '')[:60]}*", f"`{' · '.join(meta)}`"]
        if it.get("page_id"):
            lines.append(f"<{notion_url(it['page_id'])}|👉 Notion에서 보기>")
        blocks.append(_section("\n".join(lines)))

    _post({"text": header, "blocks": blocks[:MAX_BLOCKS]})
    log.info(f"Slack 알림 전송: 보류 {len(fresh)}건")
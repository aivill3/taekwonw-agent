#!/usr/bin/env python
"""TaekwonW 뉴스 블로그 자동화 — CLI 진입점.

    py run.py collect                수집 → 키워드별 선정 → 소주제 → 묶기
                                     → 초안 → Content 저장 → 삽화 (한 번에)
    py run.py collect --no-images    삽화 없이 초안까지만 (로컬 CPU 에서 빠르게)
    py run.py images                 초안에 삽화만 나중에 붙이기
    py run.py images --redo          이미 붙은 삽화를 지우고 다시 만들기
    py run.py check --file draft.md  파일 하나를 품질 검사 (파이프라인과 무관)

사람 승인 없이 collect 한 번으로 초안까지 나간다 (2026-09-17~).
결과는 Notion Content DB 보드에서 검색키워드별로, 네이버 검색 순서대로 본다.

예전의 content · confirm · publish · subtopic 명령은 없앴다. 뉴스 DB 보드에
카드를 배치하고 버튼으로 초안을 요청하던 경로라, 승인 게이트와 뉴스 보드를
없애면서 할 일이 사라졌다.

--dry-run 은 선정·묶기 결과만 보여 준다. Notion 에 쓰지 않고 LLM 도 부르지
않는다. 소주제·초안 품질까지 보려면 --no-notion 을 쓴다 (LLM 은 호출한다).
"""
import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 넣는다. 어느 디렉터리에서 실행하든
# `from config...` 가 동작하게 하기 위해서다.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _cmd_collect(args) -> None:
    from workflows import collect_workflow

    collect_workflow.run(
        dry_run=args.dry_run,
        skip_duplicates=not args.no_skip_duplicates,
        skip_subtopic=args.no_subtopic,
        no_notion=args.no_notion,
        with_images=not args.no_images,
        with_quality=not args.no_quality,
    )


def _cmd_images(args) -> None:
    from workflows import image_workflow

    image_workflow.run(
        dry_run=args.dry_run, limit=args.limit, days=args.days, redo=args.redo
    )


def _cmd_check(args) -> None:
    """파일 하나를 품질 검사한다. Notion 도 LLM 도 건드리지 않는다."""
    from core.logger import setup
    from agents.quality.quality_agent import QualityChecker, format_report

    setup()
    path = Path(args.file)
    if not path.exists():
        print(f"파일이 없습니다: {path}")
        raise SystemExit(1)

    text = path.read_text(encoding="utf-8")
    checker = QualityChecker()
    report = checker.check(text, keyword=args.keyword)
    print(format_report(report))
    # 통과하지 못하면 0이 아닌 코드로 끝낸다. CI 나 배치에서 쓸 수 있게.
    raise SystemExit(0 if report.passed else 2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="태권도 뉴스 블로그 자동화 파이프라인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 모든 하위 명령이 공유하는 옵션
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dry-run", action="store_true",
        help="Notion에 쓰지 않고 LLM도 부르지 않는다 (선정·묶기 결과만 출력)",
    )

    p = sub.add_parser(
        "collect", parents=[common], help="수집부터 초안 저장·삽화까지 자동 실행"
    )
    p.add_argument(
        "--no-subtopic", action="store_true",
        help="선정까지만 하고 끝낸다 (LLM 미호출, CSV 백업만 남긴다)",
    )
    p.add_argument(
        "--no-skip-duplicates", action="store_true",
        help="처리 이력을 무시하고 이미 쓴 기사도 다시 후보로 본다",
    )
    p.add_argument(
        "--no-notion", action="store_true",
        help="초안까지 만들어 콘솔에 출력한다. Notion 과 처리 이력은 건드리지 않는다",
    )
    p.add_argument(
        "--no-images", action="store_true",
        help="삽화를 만들지 않는다 (나중에 `run.py images`로 채울 수 있다)",
    )
    p.add_argument("--no-quality", action="store_true", help="품질 측정을 건너뛴다")
    p.set_defaults(func=_cmd_collect)

    p = sub.add_parser("images", parents=[common], help="작성된 초안에 삽화 추가")
    p.add_argument("--limit", type=int, default=0, help="처리할 글 수 상한 (0=제한 없음)")
    p.add_argument(
        "--days", type=int, default=1,
        help="며칠 이내 작성분만 대상으로 할지 (0=전체, 기본 1=오늘)",
    )
    p.add_argument(
        "--redo", action="store_true",
        help="이미 붙은 삽화를 지우고 다시 만든다 (원문 기사의 사진은 건드리지 않는다)",
    )
    p.set_defaults(func=_cmd_images)

    p = sub.add_parser("check", help="파일 하나를 품질 검사")
    p.add_argument("--file", required=True, help="검사할 마크다운/텍스트 파일")
    p.add_argument("--keyword", default=None, help="SEO 검사에 쓸 주요 키워드")
    p.set_defaults(func=_cmd_check)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
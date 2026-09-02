#!/usr/bin/env python
"""TaekwonW 뉴스 블로그 자동화 — CLI 진입점.

    py run.py collect                수집 → 정제 → Notion → 랭킹 → 소주제
    py run.py content                기사를 묶어 보드의 '묶음' 슬롯에 배정
      [사람이 Notion 보드에서 카드를 끌어 배치 조정]
    py run.py confirm --check        배치 점검 — 카드에 승인 가능/불가 표시
    py run.py confirm                배치를 검증해 '초안대기'로 확정
    py run.py publish --limit 3      확정분으로 초안 작성 → Content 생성 → 삽화
    py run.py images                 초안에 삽화만 나중에 붙이기
    py run.py images --redo          이미 붙은 삽화를 지우고 다시 만들기

    py run.py subtopic               '선정요청' 기사에 소주제 생성 (보조 경로)
    py run.py check --file draft.md  파일 하나를 품질 검사 (파이프라인과 무관)

collect 가 소주제까지 만든다. 이름과 달리 수집만 하지 않는다 — 묶기 단계가
소주제 수로 챕터를 세기 때문에 그 전에 채워져 있어야 한다.

content 와 confirm 이 나뉜 이유는 사람이 손볼 여지를 남기기 위해서다.
content 가 자동으로 묶어 보드에 배치하면, 사람이 카드를 끌어 고치고,
confirm 이 그 결과를 검증해 묶음ID 를 붙인다.

Notion 보드는 "한 칸에 4개까지"를 막지 못하므로 confirm 이 검사한다.
규칙을 어긴 슬롯은 통째로 건너뛰고 상태를 바꾸지 않으니, 고쳐서 다시
확정하면 된다.

승인은 보드에 카드를 놓고 confirm 을 돌리는 것 자체다. 별도의 승인
체크박스는 없앴다. 확정을 취소하려면 보드에서 카드를 '선정됨'으로
되돌리면 된다.

publish 를 confirm 과 합치지 않은 것은 실행 시점을 고르기 위해서다.
Gemini 2.0 Flash 는 RPD 20 이라 확정해 둔 것을 하루에 다 돌릴 수 없다.
--limit 으로 나눠 돌린다.

subtopic 은 순서상 보조 경로다. 랭킹이 놓친 기사를 사람이 Notion 에서
'선정요청' 으로 바꿔 뒀을 때만 쓴다.

--dry-run 은 Notion 에 쓰지 않고 LLM 도 부르지 않는다. 무료 티어 할당량을
아끼려는 설계라, 드라이런으로는 소주제·초안 품질을 확인할 수 없다.
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
    )


def _cmd_subtopic(args) -> None:
    from workflows import subtopic_workflow

    subtopic_workflow.run(dry_run=args.dry_run, dedupe=not args.no_dedupe)


def _cmd_content(args) -> None:
    from workflows import content_workflow

    content_workflow.run(
        dry_run=args.dry_run,
        dedupe=not args.no_dedupe,
        topic_filter=not args.no_topic_filter,
    )


def _cmd_confirm(args) -> None:
    from workflows import confirm_workflow

    confirm_workflow.run(dry_run=args.dry_run, check_only=args.check)


def _cmd_publish(args) -> None:
    from workflows import publish_workflow

    publish_workflow.run(
        dry_run=args.dry_run,
        with_images=not args.no_images,
        with_quality=not args.no_quality,
        limit=args.limit,
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
        help="Notion에 쓰지 않고 LLM도 부르지 않는다 (결과만 출력)",
    )

    p = sub.add_parser("collect", parents=[common], help="뉴스 수집 → 선정 → 소주제")
    p.add_argument(
        "--no-subtopic", action="store_true",
        help="소주제 생성을 건너뛴다 (상태는 '선정요청'으로 남는다)",
    )
    p.add_argument(
        "--no-skip-duplicates", action="store_true",
        help="이미 저장된 URL도 다시 저장한다",
    )
    p.add_argument(
        "--no-notion", action="store_true",
        help="Notion 없이 실행한다. 결과는 data/processed/ 의 CSV 로만 남는다",
    )
    p.set_defaults(func=_cmd_collect)

    p = sub.add_parser(
        "content", parents=[common], help="기사를 묶어 보드의 '묶음' 슬롯에 배정"
    )
    p.add_argument(
        "--no-dedupe", action="store_true",
        help="같은 사건 중복 제거를 하지 않는다 (여러 날치가 쌓였을 때만 끄세요)",
    )
    p.add_argument(
        "--no-topic-filter", action="store_true",
        help="주제 적합성 재검사를 하지 않는다 (사람이 고른 기사를 그대로 쓸 때)",
    )
    p.set_defaults(func=_cmd_content)

    p = sub.add_parser(
        "confirm", parents=[common], help="보드 배치를 검증해 '초안대기'로 확정"
    )
    p.add_argument(
        "--check", action="store_true",
        help="확정하지 않고 카드에 '승인 가능/불가' 문구만 기록한다",
    )
    p.set_defaults(func=_cmd_confirm)

    p = sub.add_parser("subtopic", parents=[common], help="'선정요청' 기사에 소주제 생성")
    p.add_argument("--no-dedupe", action="store_true", help="사건 묶기를 하지 않는다")
    p.set_defaults(func=_cmd_subtopic)

    p = sub.add_parser(
        "publish", parents=[common], help="확정된 묶음으로 블로그 초안 작성"
    )
    p.add_argument(
        "--no-images", action="store_true",
        help="삽화를 만들지 않는다 (나중에 `run.py images`로 채울 수 있다)",
    )
    p.add_argument("--no-quality", action="store_true", help="품질 측정을 건너뛴다")
    p.add_argument(
        "--limit", type=int, default=None,
        help="이번 실행에서 처리할 묶음 수 (Gemini RPD 20 조절용, 기본=전부)",
    )
    # --no-bundle 은 없앴다. 묶기가 content 단계로 옮겨가 publish 에서는
    # 할 일이 없다. 묶지 않고 쓰고 싶으면 Content 를 손으로 나누면 된다.
    p.set_defaults(func=_cmd_publish)

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
"""core — 프로젝트 전역에서 쓰는 기반 모듈.

    logger.py                   로깅 설정 (콘솔 + 실행별 파일)
    article_models.py           Article 데이터클래스, dedupe        [복원 필요]
    korean_morphology.py        Kiwi 형태소 분석 래퍼                [복원 필요]
    korean_morphology_types.py  형태소 통계 타입                     [복원 필요]
    prompt_loader.py            prompts/ 아래 마크다운 로더          [복원 필요]
    text_normalizer.py          마크다운 제거·정규화·문맥 추출       [복원 필요]
    state_store.py              실행 간 상태 저장                    [복원 필요]

여기 있는 모듈은 agents/ 나 workflows/ 를 임포트하지 않는다.
"""

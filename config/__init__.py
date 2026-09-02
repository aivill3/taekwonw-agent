"""설정 패키지 — 모든 튜닝 값의 단일 출처.

    settings.py         환경변수, 경로, API 자격증명
    collect_config.py   수집·랭킹 파라미터
    draft_config.py     모델 체인, 분량 임계값, 요청 간격
    quality_config.py   검사 임계값 (CheckConfig, SeoConfig)

여기 있는 모듈은 프로젝트의 어떤 것도 임포트하지 않는다. 그래야 설정이
코드에 의존하지 않고, 순환 임포트도 생기지 않는다.

값을 추가할 위치 판단:
    .env 로 바꿀 수 있어야 하는가        -> settings.py
    수집량·선별 기준인가                 -> collect_config.py
    생성 분량·모델 선택인가              -> draft_config.py
    검사 통과 기준인가                   -> quality_config.py
    특정 모듈 안에서만 쓰는 구현 상수인가 -> 그 모듈에 그대로 둔다
"""

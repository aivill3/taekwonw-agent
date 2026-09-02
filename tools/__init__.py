"""tools — 외부 서비스와 통신하는 어댑터.

    naver_news_client.py    네이버 검색 API 수집              [복원 필요]
    google_rss_client.py    구글 뉴스 RSS 수집 + URL 디코딩
    article_fetcher.py      본문 다운로드 + trafilatura 추출
    notion_store.py         Notion DB 읽기/쓰기
    slack_notifier.py       Slack Incoming Webhook 알림
    gemini_client.py        Gemini 공통 (모델 체인, 429 판정)  [복원 필요]
    diffusers_client.py     로컬 Stable Diffusion 삽화 생성    [복원 필요]
    model_state.py          모델 일일 한도 소진 상태 (실행 간 유지)

각 모듈은 자기 서비스의 프로토콜만 안다. 파이프라인 순서는 workflows/ 가 정한다.
"""

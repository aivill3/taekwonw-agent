"""기사 선정 — 보도 매체 수 + 주제 적합성 + 같은 사건 제거.

선정 순서 (2026-09-21 변경):
  사건을 묶은 뒤 '몇 개 매체가 보도했는지' 순으로 고른다. 동률이면
  네이버 검색 순서가 정한다. 코드가 거르는 것은 두 가지뿐이다.
    - 주제 적합성(is_on_topic): 태권도가 주제가 아닌 기사
    - 같은 사건(cluster_articles): 여러 매체가 보도한 사건은 1건만
  트렌드 점수로는 여전히 줄 세우지 않는다.

  왜 검색 순위만으로는 안 되는가 (2026-09-17 → 2026-09-21):
    네이버 sim 정렬은 검색어와의 문자열 관련도지 뉴스 가치가 아니다.
    제목에 검색어가 그대로 든 보도자료가 위로 오고, 본문에만 든 사건
    기사는 뒤로 밀린다. 2026-09-21 조직 레인에서 4개 매체 이상이 보도한
    사건 다섯 중 셋(경북 징계 항소·국제대회 3종 유치·세종 부강중 우승)이
    빠지고, 1개 매체짜리 병원 홍보 기사가 들어왔다.
    보도 매체 수는 이미 클러스터링이 세고 있던 값이라 새 계산이 없다.

  레인마다 따로 부른다 (collect_workflow 가 레인별 후보 목록을 나눠
  넘긴다). 한 번의 선정에 두 레인이 섞이지 않는다.
  예전의 '1위 대비 점수 하한(MIN_SCORE_RATIO)'은 뺐다. 상위 기사를
  트렌드 점수가 낮다는 이유로 버리면 순서를 따르는 의미가 없다.

  아래 트렌드 점수는 여전히 계산한다. 순서에는 쓰지 않고
    - matched_keywords: 선정 근거 표시, SEO 대표 키워드(matched_keyword)
    - score_norm: 사람이 보는 참고 점수
  에 쓴다.

트렌드 점수 (A 방식: 당일 수집 기사 자체의 빈출 키워드 기반):

핵심 아이디어:
  "여러 기사에 동시에 등장하는 키워드"가 그날의 트렌드 신호다.
  → 총 등장 횟수(TF)보다 문서 빈도(DF, 몇 개 기사에 걸쳐 나오는지)를 주 지표로 삼는다.
    한 기사에만 20번 나오는 단어보다, 5개 기사에 각 2번씩 나오는 단어가 더 강한 트렌드.

설계 원칙:
  - LLM 없이 결정론적으로 계산 (선정·랭킹은 코드의 몫)
  - 외부 의존성 0 (konlpy/JDK 불필요 — 정규식 + 불용어 사전)
  - 나중에 데이터랩 검색량(B 방식)을 얹을 수 있도록 키워드 추출과 점수 계산을 분리
    → extract_trend_keywords()의 출력이 그대로 데이터랩 조회 입력이 된다

점수 구성 (기사 1건):
  Σ [ 키워드 DF 비율 × 도메인 가중치 ] + 제목 등장 보너스

다양성 확보 (MAX_SIMILARITY):
  같은 사건을 다룬 기사들은 키워드가 겹쳐 점수도 함께 높아지므로, 점수 순으로만
  뽑으면 상위권을 한 사건이 점유한다(실측: 5건 중 3건이 동일 대회 기사).
  → 점수 높은 순으로 훑되, 이미 뽑힌 기사와 유사도가 높으면 건너뛴다.
  유사도는 '기사 전체 토큰'의 Jaccard 계수로 계산한다. 기여 키워드(최대 15개)로
  계산하면 겹침이 과대평가되어, 관점이 다른 기사까지 같은 사건으로 묶여버린다.

사건 클러스터링 + 보도 건수 가점:
  여러 매체가 같은 사건을 동시에 보도하면 뉴스 가치가 높다는 신호다.
  (네이버가 관련뉴스를 묶어 상위 노출하는 것과 같은 원리)
  → 같은 사건을 버리지 않고 묶어서, 보도 매체 수만큼 대표 기사에 가점한다.
    선정은 클러스터당 1건이므로 소재 편중은 여전히 일어나지 않는다.
  대표는 점수 1위가 아니라 '본문이 가장 충실한' 기사로 고른다
  (같은 사건이라도 사진기사와 심층기사가 섞여 들어오기 때문).

  유사도 판정에는 '사건 시그니처'를 쓴다 (트렌드 키워드가 아니다):
    트렌드 키워드(태권/대회/선수)는 모든 태권도 기사에 공통이라 사건을 구분하지 못한다.
    사건을 구분하는 것은 '베트남/호치민/장충체육관/서귀포'처럼 드물게 등장하는 고유명사다.
    → 전체 기사의 SIGNATURE_MAX_DF 이하로만 등장하는 토큰을 시그니처로 삼고
      Jaccard 계수(교집합/합집합)로 비교한다. 영문 고유명사도 포함한다.

점수의 성질 (해석 주의):
  분모가 '당일 수집 건수'이므로 값의 범위가 날마다 달라진다.
  → 날짜 간 절대 비교는 무의미하고, 같은 날 기사 사이의 순위로만 읽어야 한다.
  이 때문에 사람이 보는 지표로는 당일 1위를 100으로 환산한 score_norm을 함께 제공하고,
  선정 근거를 이해할 수 있도록 점수에 기여한 키워드(matched_keywords)도 남긴다.
"""
import os
import re
from collections import Counter
from itertools import zip_longest
from typing import Callable, TypeVar

from config.collect_config import LANES
from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)

T = TypeVar("T")

# 같은 순번일 때 어느 키워드를 앞에 둘지. LANES 에 적은 순서 그대로다.
# order_by_search() 의 동률 처리에만 쓴다.
KEYWORD_ORDER = [k for lane in LANES for k in lane.keywords]

# ── 파라미터 ───────────────────────────────────────────
TOP_N = 5              # 소주제 생성으로 넘길 상위 기사 수 (LLM 호출량 = 이 값)

# --- 주제 적합성 (절대 기준) ---
# 상대 기준(점수 비율·유사도)만으로는 후보가 적은 날 무관 기사가 통과한다.
# 후보가 2건이면 '그 중 1위'가 무조건 100점이 되기 때문이다.
# 실측 지표 (고유 도메인 키워드 수):
#   태권도가 주제인 기사      : 7 ~ 15개
#   태권도가 스쳐 언급된 기사  : 2 ~ 3개  (예: 예산 항목 중 하나, 선수 아버지 직업)
# 세 값 모두 환경변수로 조절한다. 키워드를 늘리면 통과율이 키워드마다 달라져
# (조직·협회 기사는 짧고 고유어가 적다) 실데이터로 맞춰 볼 일이 잦다.
# tools/check_ranking.py 로 수집 CSV 에 값을 바꿔 적용해 보고 정한다.
MIN_DOMAIN_HITS = int(os.getenv("MIN_DOMAIN_HITS", "5"))        # 제목에 고유어가 있을 때
MIN_DOMAIN_HITS_NO_TITLE = int(os.getenv("MIN_DOMAIN_HITS_NO_TITLE", "8"))  # 없을 때

# 태권도 고유어(CORE_KEYWORDS)가 최소 몇 개 나와야 하는지.
# 이 조건이 없으면 일반 스포츠 용어만으로 통과한다.
# 실측: 태권도 기사 4~13개 / 수영·e스포츠 기사 0~1개
# 실측(2026-08, 선정됨 43건) 분포:
#   정상 태권도 기사   3 ~ 16개  (최저 3: 킴미 기자수첩, 독일 ITF 틀투어)
#   무관 기사          0 ~ 2개   (수영·육상·e스포츠)
# 두 구간이 2와 3 사이에서 갈린다.
MIN_CORE_HITS = int(os.getenv("MIN_CORE_HITS", "3"))

# 고유어 '종류 수' 경로를 인정하려면 고유어가 원문의 서로 다른 자리에 최소
# 몇 번 나와야 하는지. 겹치거나 붙어 있는 고유어는 한 자리로 센다.
#
# 종류 수는 부분 문자열로 세서, 복합어 하나가 여러 종류로 불어난다.
#   2026-09-22 '창녕군 생활체육대회' 4건: 본문에 '태권도시범단의 식전공연'
#   한 번뿐인데 태권·태권도·시범단 3종으로 기준을 채우고 대회 레인에 뽑혔다
#   (언급 1회, tools/gate_why.py). 자리로 세면 1자리다.
#
# 1 이면 이전 동작과 같다. 값은 tools/eval_gate.py --sweep spans=1,2 로 정한다.
MIN_CORE_SPANS = int(os.getenv("MIN_CORE_SPANS", "1"))

# --- 사건·사고 기사 제외 ---
# 태권도 기사인 것은 맞지만 블로그에 실을 수 없는 종류를 거른다.
# 2026-09-18 수집분에서 '탈의실 불법촬영 관장' 기사 5건이 검색 상위
# 1·2·4·7·10위를 차지했다. 그날은 도메인 키워드 4개 < 기준 5개로 우연히
# 걸렸을 뿐이라, MIN_DOMAIN_HITS 를 내리면 그대로 들어온다. 별도 관문을 둔다.
#
# 두 층으로 나눈 이유: 형사 사건은 언제나 빼야 하지만, 협회 징계·소송은
# '태권도조직' 키워드가 실제로 물어오는 주된 소재라 빼면 그 레인이 비어버린다.
# 어느 쪽을 쓸지는 운영 판단이므로 환경변수로 갈라 둔다.
EXCLUDE_CRIME = os.getenv("EXCLUDE_CRIME", "true").strip().lower() not in (
    "0", "false", "no",
)
EXCLUDE_DISPUTE = os.getenv("EXCLUDE_DISPUTE", "false").strip().lower() in (
    "1", "true", "yes",
)

# 본문에만 나올 때 몇 개가 겹쳐야 사건 기사로 보는지.
# 제목에 하나라도 있으면 바로 제외하고, 본문만이면 스쳐 지나간 언급
# (대회 기사 끝의 '음주운전 자정 결의' 한 줄 등)일 수 있어 여러 개를 요구한다.
CRIME_BODY_HITS = int(os.getenv("CRIME_BODY_HITS", "3"))

# 형사 사건. '경찰', '검찰', '사기'는 넣지 않는다 — 경찰청장기·검찰총장기
# 대회명과 '사기진작'에 걸려 정상 기사가 탈락한다(2026-09-18 검증).
CRIME_KEYWORDS = {
    # 성범죄
    "불법촬영", "몰카", "성추행", "강제추행", "성폭행", "성폭력", "성착취",
    "성범죄", "음란물", "성희롱",
    # 폭력·기타 범죄
    # '학대' 단독은 뺐다. '보건과학대', '의과대학' 같은 학교명에 걸린다
    # (2026-09-21: 충북보건과학대 합동훈련 기사가 사건 기사로 탈락).
    # 복합어로만 잡는다. 공백은 지우고 보므로 '선수 학대 혐의'도 걸린다.
    "아동학대", "노인학대", "동물학대", "학대혐의", "학대의혹", "학대정황",
    "폭행", "감금", "협박", "가혹행위", "마약", "음주운전", "뺑소니",
    "상해죄", "사기혐의", "사기죄", "횡령", "배임",
    # 수사·재판 절차
    "징역", "구형", "실형", "집행유예", "기소", "구속영장", "송치", "입건",
    "체포", "피의자", "유죄", "선고공판", "신상공개", "혐의",
}

# 협회 분쟁·징계·소송. 기본은 통과시킨다(EXCLUDE_DISPUTE=false).
# 켜면 '태권도조직' 레인에 남는 기사가 거의 없어진다. 2026-09-18 기준
# 그 레인의 유일한 고유 사건이 경북태권도협회 징계 분쟁이었다.
DISPUTE_KEYWORDS = {
    "징계", "항소심", "소송", "가처분", "고소", "고발", "제명", "직무정지",
    "자격정지", "비리", "법정공방", "사퇴촉구", "집단행동", "갈등",
}

_RE_SPACE = re.compile(r"\s+")

# 태권도가 스쳐 지나가는 종합 기사(브리핑 모음, 패트롤) 방어.
# 6,000자짜리 지자체 종합 기사는 태권도가 5%만 차지해도 고유어가
# 개수 기준을 넘는다. 그래서 '길이 대비 밀도'도 함께 본다.
# 1,000자당 고유어가 이 값 미만이면 제외한다.
MIN_CORE_DENSITY = 0.8
# 제목에 태권도 고유어가 없을 때는 더 높은 밀도를 요구한다.
# 태권도가 주제인 기사는 제목에 거의 반드시 나온다. 제목에 없는데 본문만
# 길다면 '태권도가 한 꼭지로 들어간 종합 기사'일 가능성이 높다.
#
# 실측 분포 (제목X 기사만):
#   정상   3.05 이상  (MBC 국제오픈태권도대회 폐막이 최저)
#   무관   1.05 ~ 1.68 (육상대회, 용인시 선수단 종합)
# 두 구간 사이가 비어 있어 2.0으로 잡는다.
MIN_CORE_DENSITY_NO_TITLE = 2.0

# --- 핵심어 '언급 횟수' 경로 ---
# 종류 수가 낮아도 언급이 잦으면 주제 기사로 본다. 협회·조직 기사는
# '태권도협회' 한 단어만 반복해 종류 수가 1~2로 깔린다.
# 실측 (2026-09-18 수집분 29건):
#   조직·협회 기사   4 ~ 16회
#   대회·행사 기사   4 ~ 22회
#   무관 기사        1회    (ITF 테니스, 여수세계섬박람회 — 둘 다 1,700자 이상)
MIN_CORE_MENTIONS = int(os.getenv("MIN_CORE_MENTIONS", "4"))

# 언급 밀도(1,000자당). 긴 종합 기사가 횟수만으로 통과하는 것을 막는다.
# 실측: 무관 0.55 / 0.57 · 정상 2.69 이상 — 두 구간 사이.
MIN_MENTION_DENSITY = 1.5
MIN_MENTION_DENSITY_NO_TITLE = 3.0

# 밀도 검사를 적용할 최소 본문 길이.
# 예전에는 2,000자였는데, 그 아래 기사가 검사를 통째로 건너뛰면서
# 1,908자짜리 육상 기사와 1,466자짜리 e스포츠 기사가 통과했다.
# 짧은 정상 기사는 밀도가 오히려 높으므로(494자 기사가 10.12) 낮춰도 안전하다.
DENSITY_CHECK_MIN_CHARS = 600

# 챕터 하나를 쓸 수 있는 최소 원문 길이.
# 통신사 사진 기사는 캡션 한 줄이 전부라 밀도가 20 이상으로 치솟고,
# 밀도 검사는 DENSITY_CHECK_MIN_CHARS 미만이면 건너뛰므로 모든 관문을
# 그냥 통과한다. 그런데 원문이 모자라 LLM 이 챕터를 지어내게 된다.
# 실측(2026-09-18): 사진 캡션 기사 119·131·136자 / 정상 기사 최소 338자.
MIN_BODY_CHARS = int(os.getenv("MIN_BODY_CHARS", "250"))
# --- 사건 클러스터링 / 보도 건수 가점 ---
# 여러 매체가 같은 사건을 동시에 보도하면 뉴스 가치가 높다는 신호다.
# (네이버가 관련뉴스를 묶어 상위 노출하는 것과 같은 원리)
# 그래서 같은 사건을 '제외'하는 대신, 묶어서 가점을 주고 대표 1건만 선정한다.
CLUSTER_BONUS = 0.35   # 보도 매체가 1곳 늘어날 때마다 점수에 곱해지는 가산 비율
CLUSTER_BONUS_MAX = 1.5  # 가점 상한 (한 사건이 상위를 독식하지 않도록)

# 선정 순서에서 '몇 개 매체 이상을 앞줄로 볼지'. 한 값으로 세 방식을 고른다.
#   0     보도 매체 수 내림차순 (많이 보도된 사건이 무조건 앞)
#   1     검색 순서 그대로 (매체 수를 순서에 쓰지 않음 — 2026-09-17 방식)
#   N≥2   N개 매체 이상을 앞줄로, 앞줄 안에서는 검색 순서
#
# 기본 3인 이유: 2매체는 홍보대행사 동시 배포와 구분되지 않는다.
# (2026-09-21 태권도 레인: 2매체 사건 셋이 2~4번을 차지했고 그중 하나가
#  검색 41위 한복 체험 포토존 홍보 기사였다. 같은 날 조직 레인은 4매체
#  이상 사건이 일곱 개라 이 값에 영향받지 않는다)
CLUSTER_PRIORITY_MIN = int(os.getenv("CLUSTER_PRIORITY_MIN", "3"))

# 시그니처 유사도가 이 이상이면 같은 사건으로 묶는다.
# 실측 분포: 같은 사건 0.26~0.31, 다른 사건 0.00~0.04 → 두 구간 사이 값.
# 다만 같은 행사를 각기 다른 각도로 쓴 기사(행사 예고·개최·참가자 반응)는
# 고유명사가 덜 겹쳐 0.1 언저리로 나온다. 한 편에 같은 행사가 여러 챕터로
# 들어가면 EVENT_SIMILARITY 를 0.10 근처로 낮춘다. 반대로 사건 수가 적은
# 날 후보가 부족하면 0.2~0.25 로 올린다.
MAX_SIMILARITY = float(os.getenv("EVENT_SIMILARITY", "0.15"))

# 겹침 계수(공유 토큰 / 작은 쪽 시그니처 크기) 기준. Jaccard 와 '또는' 으로 쓴다.
#
# Jaccard 는 길이가 크게 다른 두 기사에서 낮게 나온다. 합집합이 긴 쪽 토큰으로
# 채워져, 짧은 쪽 토큰이 거의 다 겹쳐도 값이 작다. 종합 기사 1건과 선수 개인
# 기사 여럿이 같은 대회를 다룰 때 이 경우가 된다.
#   실측(2026-09-22 수집 131건, tools/event_pairs.py --near):
#     J < 0.12 이면서 O ≥ 0.5 인 쌍 8개가 전부 같은 사건이었다.
#     그중 5개는 4,649자 품새 14연패 종합 기사와 200~700자 선수별 기사.
#     예) 14연패 종합 vs 14회 연속 종합우승 단신  J 0.104 · O 0.810
#
# 0 이면 끈다 (Jaccard 만 쓰던 동작으로 돌아가는 손잡이).
EVENT_OVERLAP = float(os.getenv("EVENT_OVERLAP", "0.5"))
# 겹침 계수를 쓸 때 필요한 최소 공유 토큰 수. 시그니처가 아주 작은 기사(캡션·
# 단신)는 일반어 몇 개만 겹쳐도 비율이 높게 나온다. 위 8쌍의 공유 토큰은
# 최소 19개였다.
EVENT_OVERLAP_MIN_SHARED = int(os.getenv("EVENT_OVERLAP_MIN_SHARED", "15"))
SIGNATURE_MAX_DF = 0.5 # 전체 기사의 이 비율을 넘게 등장하는 토큰은 시그니처에서 제외
                       # (태권/대회/선수 같은 일반어는 사건을 구분하지 못함)
SIGNATURE_MIN_DF_CAP = 3  # 위 비율로 계산한 상한의 하한값.
                       # 후보가 적을 때 비율만 쓰면 상한이 1~2가 되어,
                       # '같은 사건이 공유하는 토큰'까지 제외돼 유사도가 0이 된다.
TREND_KEYWORD_N = 15   # 트렌드 키워드 후보 수 (B 방식의 데이터랩 조회 대상이 됨)
MIN_DF = 2             # 최소 2개 기사에 등장해야 트렌드로 인정
DOMAIN_WEIGHT = 2.0    # 태권도 도메인 키워드 가중치
TITLE_BONUS = 1.5      # 제목에 등장하는 키워드 가중치 배수


# 사진·영상 기사 제목 표지. 대표로 뽑히면 초안 소스 본문이 캡션 몇 줄이 된다.
# (2026-09-22: 7개 매체가 보도한 조직 사건의 대표로 '[포토뉴스] 춘천시·세계
#  태권도연맹…'이 뽑혔다. 사진 여러 장에 캡션이 달리면 본문 길이만으로는
#  단신 기사를 이긴다 — 길이로는 구분되지 않는다)
CAPTION_TAGS = ("포토", "사진", "화보", "영상", "카드뉴스", "그래픽", "만평")
_CAPTION_BRACKET = re.compile(r"[\[\(【]([^\]\)】]{1,12})[\]\)】]")


def is_caption_title(title: str) -> bool:
    """제목의 괄호 표지로 사진·영상 기사를 가려낸다.

    제목 앞뒤 어디에 붙어도 잡는다. '[포토] 제목' 과 '제목 (사진)' 이
    둘 다 쓰이기 때문이다. 괄호 안 12자 제한은 본문 인용구를 잘못
    잡지 않으려는 것이다.
    """
    return any(
        tag in m.group(1)
        for m in _CAPTION_BRACKET.finditer(title)
        for tag in CAPTION_TAGS
    )

def threshold_summary() -> str:
    """이번 실행의 선정 임계값 한 줄. 로그와 채점 도구가 같은 문자열을 쓴다.

    왜 필요한가
    ----------
    임계값은 .env(로컬)와 collect.yml(GitHub Actions) 두 곳에서 따로
    주입된다. 한쪽만 고치면 로컬 dry-run 과 실제 발행이 다른 설정으로
    돌고, 로그만 봐서는 그 사실을 알 수 없다. 어느 값으로 돈 회차인지
    로그에 남겨 두면 두 로그를 나란히 놓고 대조할 수 있다.

    tools/eval_gate.py 도 이 함수를 부른다. 채점 결과와 실제 실행이 같은
    설정이었는지 확인하려면 두 문자열이 같은 코드에서 나와야 한다.
    (eval_gate 는 importlib.reload 로 이 모듈을 다시 읽으므로, 스윕 중에도
     그 조합의 값이 그대로 나온다)

    ⚠️ 새 임계값을 추가하면 여기도 함께 고칠 것. 목록에서 빠진 값은 로그에
       안 남고, 안 남으면 두 설정이 어긋나도 모른다.

    os.getenv 상수를 자동으로 긁지 않는 이유: TREND_KEYWORD_N, MIN_DF 처럼
    운영자가 건드리지 않는 내부 상수까지 섞여 줄이 길어진다. 무엇을 보여줄지
    고르는 일은 손으로 하는 편이 낫다.
    """
    return (
        f"core={MIN_CORE_HITS} · spans={MIN_CORE_SPANS} · mentions={MIN_CORE_MENTIONS} "
        f"· body={MIN_BODY_CHARS} · domain={MIN_DOMAIN_HITS}"
        f"/{MIN_DOMAIN_HITS_NO_TITLE} · similarity={MAX_SIMILARITY} "
        f"· overlap={EVENT_OVERLAP}/{EVENT_OVERLAP_MIN_SHARED} "
        f"· cluster_min={CLUSTER_PRIORITY_MIN} "
        f"· crime={EXCLUDE_CRIME} · dispute={EXCLUDE_DISPUTE}"
    )

# ── 태권도 도메인 사전 (2층 구조) ──────────────────────
# 예전에는 한 덩어리였다. 그러다 보니 '대회·선수·감독·금메달' 같은 일반
# 스포츠 용어가 태권도 신호로 계산돼, 태권도가 한 글자도 없는 수영·육상·
# e스포츠 기사가 도메인 키워드 16개로 통과했다.
# (실측: 수영 기사 16개 > 진짜 태권도 기사 13개)
#
# 그래서 '태권도에서만 쓰는 말'과 '어느 종목에나 나오는 말'을 나눈다.
# 통과 판정은 CORE 를 기준으로 하고, GENERIC 은 점수 가중치에만 쓴다.

# 태권도 기사에만 나오는 말. 다른 종목 기사에는 거의 등장하지 않는다.
# '띠' 는 뺐다. 원문 직접 검색으로 바꾼 뒤(2026-09-21) '의미를 띠고',
# '긴장감을 띠었다' 같은 용언이 전부 고유어로 잡힌다. 한 글자 사전어는
# 부분 문자열 검사와 맞지 않는다. 대신 '검은띠' 를 넣는다.
CORE_KEYWORDS = {
    # 종목·기술
    "태권도", "태권", "겨루기", "품새", "격파", "발차기", "도복", "검은띠",
    # 기관·단체
    "국기원", "세계태권도연맹", "태권도협회", "시범단", "태권도장", "도장",
    # 심사·자격
    "승품심사", "승단심사", "단증", "품증", "사범", "관장", "유단자", "수련생",
    "공인단", "겨루기장", "품새복",
}

# 스포츠 기사면 종목과 무관하게 나오는 말. 점수 가중치에만 쓰고
# 주제 적합성 판정에는 쓰지 않는다.
GENERIC_KEYWORDS = {
    "협회", "연맹", "지도자", "심판",
    "대회", "선수권", "챔피언십", "올림픽", "아시안게임", "전국체전", "예선", "결선",
    "금메달", "은메달", "동메달", "우승", "준우승", "입상",
    "선수", "국가대표", "대표팀", "감독", "코치",
    "시범", "공연", "훈련", "수련", "합동훈련", "교류", "보급", "협약", "체결",
}

# 가중치 계산용 (기존 DOMAIN_KEYWORDS 와 같은 역할)
DOMAIN_KEYWORDS = CORE_KEYWORDS | GENERIC_KEYWORDS

# ── 불용어 ─────────────────────────────────────────────
STOPWORDS = {
    # 일반 명사·부사
    "이번", "지난", "올해", "내년", "작년", "현재", "최근", "당시", "오늘", "내일",
    "그동안", "앞으로", "이날", "이후", "이전", "가운데", "관련", "통해", "위해",
    "대해", "따라", "면서", "라며", "라고", "했다", "이라고", "있다", "없다", "된다",
    "우리", "저희", "자신", "모두", "각각", "여러", "다른", "같은", "많은", "새로운",
    "경우", "때문", "정도", "수준", "방법", "내용", "부분", "결과", "과정", "상황",
    "이라며", "밝혔다", "말했다", "전했다", "덧붙였다", "설명했다", "강조했다",
    # 뉴스 상용어
    "기자", "특파원", "뉴스", "기사", "보도", "취재", "인터뷰", "본보", "연합뉴스",
    "오전", "오후", "예정", "계획", "진행", "개최", "실시", "참가", "참여", "출전",
    # 행정·일반
    "지역", "국내", "해외", "전국", "서울", "정부", "시장", "군수", "시청", "군청",
    "이상", "이하", "미만", "초과", "약간", "매우", "정말", "특히", "또한", "그리고",
    # 실사용에서 무관 기사를 끌어올린 일반어 (2026-07 로그 기준)
    "세계", "경쟁력", "대표", "오는", "지난해", "올해", "내년", "당일", "행사",
    "개최지", "관계자", "가능성", "필요성", "중요성", "역할", "의미", "가치",
    "문화", "교육", "사업", "지원", "운영", "추진", "협력", "구축", "확대",
}

# 2~5글자 한글 어절 (조사가 붙은 형태까지 포함해 뽑고 뒤에서 정리)
RE_TOKEN = re.compile(r"[가-힣]{2,5}")

# 용언·수식어 종결 (명사만 남기기 위해 제외 — 형태소 분석기 없이 근사)
# '있도록', '하도록' 같은 ~도록 어미와 '일까지', '중에서' 같은 형태가
# 트렌드 키워드로 잡히던 것을 추가로 막는다.
# (실측 로그: 있도록(0.45), 일까지(0.45) — 태권도(0.70)의 3분의 2 가중치)
RE_VERB_TAIL = re.compile(
    r"(?:했다|한다|하는|하고|하며|해서|이다|되다|된다|되는|됐다|있는|있다|없는|없다"
    r"|였다|이라|으로|위한|위해|대한|따른|같은|다른|많은|모든"
    r"|도록|면서|지만|는데|으며|어야|아야|고자|려고|거나"
    # _strip_particles 가 잘라내면 2자 미만이 되어 남는 형태.
    # ('일까지' -> 어간 '일'이 1자라 절단 불가 -> 여기서 버린다)
    r"|까지|부터|마다|처럼|조차)$"
)

# 흔한 조사·어미 (어절 끝에서 잘라냄 — 형태소 분석기 없이 근사)
PARTICLES = (
    "으로서", "에서는", "으로는", "이라는", "라는", "에게", "에서", "으로", "이나",
    # 시간·범위 조사. '일까지', '일부터'가 트렌드 키워드로 잡히던 원인.
    "까지", "부터", "에서도", "에도", "마다", "처럼", "보다", "조차", "밖에",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만", "로",
)


def _strip_particles(token: str) -> str:
    """어절 끝의 조사를 잘라낸다. 3글자 미만이 되면 원형을 유지 (과절단 방지)."""
    for p in PARTICLES:
        if token.endswith(p) and len(token) - len(p) >= 2:
            return token[: -len(p)]
    return token


def tokenize(text: str) -> list[str]:
    """한글 어절 추출 → 조사 제거 → 불용어/용언 제외.

    한계: 형태소 분석기가 아니므로 6글자 이상 고유명사는 잘려 나온다
    (예: '세이브더칠드런코리아' → 부분 토큰). 트렌드 판정은 여러 기사에
    반복 등장하는지를 보는 것이므로, 같은 방식으로 잘린 토큰끼리
    일관되게 매칭돼 실용상 문제가 되지 않는다."""
    tokens = []
    for raw in RE_TOKEN.findall(text):
        t = _strip_particles(raw)
        if len(t) < 2 or t in STOPWORDS:
            continue
        if RE_VERB_TAIL.search(t):
            continue  # 용언·수식어 제외 (명사 키워드만 남김)
        tokens.append(t)
    return tokens


def _matches(keyword: str, vocab: set[str]) -> bool:
    """사전 단어를 '포함'하는지. ('태권도장' ⊃ '태권도' → True)

    예전에는 반대 방향(keyword in d)도 인정했다. 그래서 사전어의 일부만
    겹쳐도 통과해 범위가 폭발했다.

        게임 ⊂ 아시안게임        -> e스포츠 기사가 통과
        세계 ⊂ 세계태권도연맹     -> 아무 국제 기사나 통과
        메달 ⊂ 금메달
        국기 ⊂ 국기원
        대표 ⊂ 대표팀
        체전 ⊂ 전국체전

    '아시안게임'을 사전에 넣은 대가로 '게임'이 태권도 신호가 된 셈이다.
    한 방향만 인정하면 이 오탐이 한꺼번에 사라진다.
    """
    return any(d in keyword for d in vocab)


def _is_domain(keyword: str) -> bool:
    """도메인 사전 단어를 포함하는지 (가중치 계산용, CORE + GENERIC)."""
    return _matches(keyword, DOMAIN_KEYWORDS)


# _is_core() 는 지웠다. 주제 적합성 판정이 토큰이 아니라 원문 직접 검색으로
# 바뀌면서(2026-09-21) 호출부가 사라졌다. _is_domain() 은 트렌드 가중치
# 계산에서 여전히 토큰 단위로 쓰인다.


def _match_text(article: Article) -> str:
    """사전 대조용 원문. 제목과 본문을 줄바꿈으로 잇는다.

    붙이지 않고 줄바꿈을 두는 이유: 경계에서 없던 단어가 생기는 것을 막는다.
    공백도 그대로 둔다. _squash() 처럼 공백을 없애면 '강원도 장학금' 이
    '강원도장학금' 이 되어 '도장' 이 걸린다. 지역 뉴스에서 흔한 형태라
    잃는 것(= '태권도 협회' 띄어쓰기)보다 얻는 오탐이 크다.
    '태권도 협회' 는 '태권도' 가 이미 잡고, 언급 횟수 경로도 받아준다.
    """
    return f"{article.title}\n{article.body_clean}"


def _count_words(text: str, vocab: set[str]) -> int:
    """원문에 등장하는 사전 단어의 '종류 수'.

    토큰이 아니라 원문에서 직접 찾는다. RE_TOKEN 이 한글을 5글자씩 자르는
    탓에, 대회 공식명이 길수록 고유어가 적게 잡히는 편향이 있었다.

        세계태권도품새선수권대회
          → 세계태권도 / 품새선수권 / 대회   (5+5+2 로 잘림)
          → 조사 '도' 가 떨어져 '세계태권'
          → core 토큰 2종 (세계태권 ⊃ 태권, 품새선수권 ⊃ 품새)

    2026-09-21: 이 때문에 '춘천 2026 세계태권도품새선수권대회 마무리' 가
    2종으로 탈락하고, '태권도' 가 독립 어절로 나온 피트니스 축제 기사가
    통과했다. 가장 태권도다운 기사가 떨어지는 구조적 편향이었다.

    주의: '태권' 은 '태권도' 의 부분 문자열이라 거의 모든 기사에 +1 로
    깔린다. MIN_CORE_HITS 3 은 실질적으로 '태권도 + 다른 고유어 하나' 다.
    """
    return sum(1 for w in vocab if w in text)


def count_domain_hits(article: Article) -> int:
    """기사에 등장하는 '고유' 도메인(태권도) 키워드 수.

    트렌드 점수만으로는 태권도와 무관한 기사가 일반어 매칭으로 상위에 올 수 있다.
    이 값으로 주제 적합성을 별도 검증한다.

    토큰이 아니라 원문에서 직접 센다. 이유는 _count_words() 참고."""
    return _count_words(_match_text(article), DOMAIN_KEYWORDS)


def count_core_hits(article: Article) -> int:
    """기사에 등장하는 '고유' 태권도 전용 키워드 수.

    일반 스포츠 용어를 뺀 값이라, 종목을 구분하는 실질 신호다.

    토큰이 아니라 원문에서 직접 센다. 이유는 _count_words() 참고.
    """
    return _count_words(_match_text(article), CORE_KEYWORDS)


def count_core_spans(article: Article) -> int:
    """고유어가 원문의 몇 '자리' 에 나오는지.

    고유어가 나온 구간을 모두 모아, 겹치거나 맞닿은 구간은 하나로 합친다.
    '태권도시범단' 은 태권[0,2)·태권도[0,3)·시범단[3,6) 이 이어져 1자리다.
    종류 수(count_core_hits)가 복합어 하나로 불어나는 것을 가려낸다.
    """
    text = _match_text(article)
    spans: list[tuple[int, int]] = []
    for w in CORE_KEYWORDS:
        i = text.find(w)
        while i >= 0:
            spans.append((i, i + len(w)))
            i = text.find(w, i + 1)
    spans.sort()
    count, end = 0, -1
    for s, e in spans:
        if s > end:
            count += 1
            end = e
        else:
            end = max(end, e)
    return count


def count_core_mentions(article: Article) -> int:
    """핵심어가 몇 '번' 나오는지. count_core_hits() 의 종류 수와 다르다.

    '태권'만 센다. 태권도·태권도협회·경북태권도협회·태권도장이 모두 여기에
    걸리고, 조사나 띄어쓰기 변형에 영향받지 않는다.

    협회·조직 기사는 '태권도협회' 한 단어만 반복해 종류 수가 1~2로 낮지만,
    그 한 단어가 본문 전체에 깔린다. 종류 수만으로는 주제 기사인데 탈락한다.
    (2026-09-18: 경북체육회 징계 기사 4건이 고유어 2개로 탈락)
    """
    return f"{article.title}\n{article.body_clean}".count("태권")


def has_domain_in_title(article: Article) -> bool:
    """제목에 도메인(태권도) 키워드가 있는지.

    실측상 가장 강한 판별 신호다. 태권도가 주제인 기사는 제목에 태권도/국기원/
    겨루기 등이 거의 반드시 등장하고, 스쳐 언급된 기사는 제목에 나오지 않는다.
    (예: '무주군, 국가예산 확보 총력' / '한여름 끓어오르는 KIA 톱타자')

    CORE 기준으로 본다. 일반어까지 인정하면 '나고야 아시안게임' 제목이
    '제목O'로 판정돼 기준이 8개에서 5개로 완화된다. 제목 조건이 오히려
    무관 기사의 통과를 도왔던 원인이다.

    여기도 토큰이 아니라 제목 원문에서 직접 찾는다. 제목은 대회 공식명이
    통째로 들어가는 자리라 토큰 절단의 영향이 가장 크다.
    """
    return any(w in article.title for w in CORE_KEYWORDS)


def _squash(text: str) -> str:
    """공백을 없앤다. '불법 촬영'과 '불법촬영'을 같은 말로 보기 위해서다.

    tokenize() 를 쓰지 않는 이유: 어절 단위로 잘려 '불법 촬영'이 두 토큰이
    되고, 조사 제거 과정에서 형태도 달라진다. 사전어가 짧고 구체적이라
    부분 문자열 검사가 더 안전하다.
    """
    return _RE_SPACE.sub("", text)


def find_incident_words(article: Article) -> tuple[list[str], list[str]]:
    """기사에서 발견된 사건어를 (제목, 본문) 으로 나눠 반환한다.

    본문 목록에서는 제목에 이미 있는 말을 뺀다. 같은 말을 두 번 세면
    본문 기준(CRIME_BODY_HITS)이 실제보다 쉽게 채워진다.
    """
    words = (CRIME_KEYWORDS if EXCLUDE_CRIME else set()) | (
        DISPUTE_KEYWORDS if EXCLUDE_DISPUTE else set()
    )
    if not words:
        return [], []
    sq_title, sq_body = _squash(article.title), _squash(article.body_clean)

    def hit(word: str, raw: str, squashed: str) -> bool:
        """두 글자 사건어는 원문에서, 세 글자 이상은 공백 제거본에서 찾는다.

        공백 제거는 '불법 촬영'을 잡으려고 넣은 것인데, 두 글자 말에는
        오히려 없던 말을 만들어 낸다. 실측(2026-09-21):

            '경기 소식과 단체 포상'  → '기소', '체포'
            '수입 건수 증가'        → '입건'

        스포츠 기사에서 '경기 소식'은 흔한 표현이라 그대로 두면 정상
        기사가 사건 기사로 탈락한다. 두 글자 말은 붙여 쓰는 것이 보통이라
        원문에서 찾아도 놓치지 않는다.
        """
        return word in (raw if len(word) <= 2 else squashed)

    in_title = sorted(w for w in words if hit(w, article.title, sq_title))
    in_body = sorted(
        w for w in words
        if hit(w, article.body_clean, sq_body) and w not in in_title
    )
    return in_title, in_body


def is_incident(article: Article) -> tuple[bool, str]:
    """블로그에 실을 수 없는 사건 기사인지. (해당여부, 사유)

    제목은 기사의 주제를 그대로 드러내므로 한 개로 충분하다.
    본문은 CRIME_BODY_HITS 개 이상 겹칠 때만 본다.
    """
    in_title, in_body = find_incident_words(article)
    if in_title:
        return True, f"사건 기사 — 제목에 {', '.join(in_title[:3])}"
    if len(in_body) >= CRIME_BODY_HITS:
        return True, f"사건 기사 — 본문에 {', '.join(in_body[:4])} 등 {len(in_body)}개"
    return False, ""


def is_on_topic(article: Article) -> tuple[bool, str]:
    """태권도가 '주제'인 기사인지 판정. (통과여부, 사유)

    상대 기준과 달리 후보 수에 영향받지 않는 절대 기준이다.
    다섯 관문을 모두 통과해야 한다.

        0) 사건 기사     범죄·수사·재판 기사는 태권도 기사여도 제외
        1) 본문 길이     사진 캡션 기사처럼 챕터를 쓸 수 없는 원문 제외
        2) 핵심어 양     고유어 '종류 수'(서로 다른 자리 MIN_CORE_SPANS 이상)
                         또는 '언급 횟수' 중 하나가 기준 이상
        3) 핵심어 밀도   긴 기사는 1,000자당 값이 일정 수준 이상
        4) 전체 개수     기존 도메인 키워드 개수 기준 (제목 유무로 완화)

    0)이 실을 수 없는 소재를, 1)이 쓸 수 없는 원문을 빼고, 2)가 종목을
    가르고, 3)이 종합 기사를 거르고, 4)가 얕은 언급을 거른다.

    0)을 맨 앞에 둔 이유: 사건 기사는 태권도 고유어가 넉넉해(관장·도장·사범)
    뒤 관문을 그냥 통과한다. 2026-09-18 의 불법촬영 기사 5건은 도메인
    키워드가 4개라 우연히 걸렸을 뿐이다.

    2)·3)에서 두 지표를 OR 로 보는 이유: 협회·조직 기사는 '태권도협회'
    한 단어만 반복해 종류 수가 1~2로 깔린다. 종류 수만 보면 주제 기사인데
    탈락한다. 실측(2026-09-18) 언급 횟수는 조직 기사 4~16회, 무관 기사 1회.
    """
    incident, why = is_incident(article)
    if incident:
        return False, why

    # 1) 사진 설명글 방어. 캡션 한 줄짜리 기사는 밀도가 20 이상으로 치솟아
    #    뒤 관문을 모두 통과하지만, 챕터 하나를 쓰기에 원문이 모자라
    #    LLM 이 없는 내용을 지어내게 된다.
    chars = len(article.body_clean)
    if chars < MIN_BODY_CHARS:
        return False, f"본문 {chars}자 / 최소 {MIN_BODY_CHARS}자 (사진 설명글 추정)"

    core = count_core_hits(article)
    spans = count_core_spans(article)
    mentions = count_core_mentions(article)
    in_title = has_domain_in_title(article)

    # 2) 종류 수 또는 언급 횟수 — 둘 중 하나만 넘으면 통과.
    #    종류 수는 대회 기사를, 언급 횟수는 조직 기사를 살린다.
    #    종류 수는 서로 다른 자리에서 나왔을 때만 인정한다 (MIN_CORE_SPANS).
    by_types = core >= MIN_CORE_HITS and spans >= MIN_CORE_SPANS
    if not by_types and mentions < MIN_CORE_MENTIONS:
        return False, (
            f"태권도 고유어 {core}종·{spans}자리 / 최소 {MIN_CORE_HITS}종·"
            f"{MIN_CORE_SPANS}자리 · 언급 {mentions}회 / 최소 {MIN_CORE_MENTIONS}회"
        )

    # 3) 종합 기사 방어: '대학 브리핑 모음', '패트롤' 같은 6,000자짜리 기사는
    #    태권도가 한 단락만 차지해도 개수는 채운다. 길이로 나눠 본다.
    #    여기서도 두 지표를 보고, 둘 다 미달일 때만 떨어뜨린다.
    if chars >= DENSITY_CHECK_MIN_CHARS:
        core_density = core / (chars / 1000)
        mention_density = mentions / (chars / 1000)
        min_core = MIN_CORE_DENSITY if in_title else MIN_CORE_DENSITY_NO_TITLE
        min_mention = (
            MIN_MENTION_DENSITY if in_title else MIN_MENTION_DENSITY_NO_TITLE
        )
        if core_density < min_core and mention_density < min_mention:
            where = "제목O" if in_title else "제목X"
            return False, (
                f"고유어 밀도 {core_density:.2f} / 기준 {min_core} · "
                f"언급 밀도 {mention_density:.2f} / 기준 {min_mention} "
                f"({where}, 본문 {chars:,}자)"
            )

    hits = count_domain_hits(article)
    required = MIN_DOMAIN_HITS if in_title else MIN_DOMAIN_HITS_NO_TITLE
    ok = hits >= required
    where = "제목O" if in_title else "제목X"
    return ok, (
        f"도메인 키워드 {hits}개 / 기준 {required}개 ({where}) · 고유어 {core}개"
    )


def extract_trend_keywords(articles: list[Article]) -> dict[str, float]:
    """당일 기사들에서 트렌드 키워드를 추출해 {키워드: 가중치}로 반환.

    가중치 = (DF / 전체 기사 수) × (도메인 사전이면 DOMAIN_WEIGHT)

    ※ 이 함수의 출력(키워드 목록)이 그대로 B 방식(데이터랩 검색량 조회)의
      입력이 된다. B를 붙일 때는 여기서 반환된 가중치에 검색량 계수를 곱하면 된다.
    """
    total = len(articles)
    if total == 0:
        return {}

    # DF: 키워드가 몇 개 기사에 등장하는지 (기사 내 중복은 1회로 계산)
    df = Counter()
    for a in articles:
        for kw in set(tokenize(f"{a.title}\n{a.body_clean}")):
            df[kw] += 1

    weights: dict[str, float] = {}
    for kw, count in df.items():
        if count < MIN_DF:
            continue  # 한 기사에만 나오는 단어는 트렌드가 아님
        w = count / total
        if _is_domain(kw):
            w *= DOMAIN_WEIGHT
        weights[kw] = w

    # 가중치 상위 N개만 유지
    top = dict(sorted(weights.items(), key=lambda x: x[1], reverse=True)[:TREND_KEYWORD_N])
    if top:
        preview = ", ".join(f"{k}({v:.2f})" for k, v in list(top.items())[:8])
        log.info(f"트렌드 키워드 {len(top)}개: {preview}")
    return top


def score_article(
    article: Article, keyword_weights: dict[str, float]
) -> tuple[float, list[str]]:
    """기사 1건의 (점수, 기여 키워드 목록).

    포함된 트렌드 키워드의 가중치 합이며 제목 등장은 보너스를 받는다.
    본문 길이에 따른 유리함을 없애기 위해 '고유 키워드' 기준으로 계산한다.
    기여 키워드는 가중치가 큰 순서로 정렬해 반환한다 (선정 근거 표시용)."""
    title_tokens = set(tokenize(article.title))
    body_tokens = set(tokenize(article.body_clean))

    score = 0.0
    contributions: list[tuple[str, float]] = []
    for kw, w in keyword_weights.items():
        if kw in title_tokens:
            gained = w * TITLE_BONUS
        elif kw in body_tokens:
            gained = w
        else:
            continue
        score += gained
        contributions.append((kw, gained))

    contributions.sort(key=lambda x: x[1], reverse=True)
    return round(score, 4), [kw for kw, _ in contributions]


# 시그니처용 토큰: 한글 어절 + 영문 단어(CJ, Vietnam 등 고유명사) + 4자리 연도
RE_TOKEN_SIG = re.compile(r"[가-힣]{2,6}|[A-Za-z]{2,}|\d{4}")


def _tokenize_signature(text: str) -> set[str]:
    """사건 식별용 토큰 집합. 조사 제거 후 불용어만 걸러낸다
    (도메인 일반어는 DF 기준으로 뒤에서 제외되므로 여기서는 남긴다)."""
    tokens = set()
    for raw in RE_TOKEN_SIG.findall(text):
        if raw.isascii():
            tokens.add(raw.lower())
            continue
        tk = _strip_particles(raw)
        if len(tk) >= 2 and tk not in STOPWORDS and not RE_VERB_TAIL.search(tk):
            tokens.add(tk)
    return tokens


def build_signatures(articles: list[Article]) -> dict[str, set[str]]:
    """기사별 사건 시그니처 계산. {url: 토큰집합}

    전체 기사의 SIGNATURE_MAX_DF 비율을 초과해 등장하는 토큰은 제외한다.
    이런 토큰(태권/대회/선수)은 어느 기사에나 있어 사건을 구분하지 못하기 때문이다.
    """
    total = len(articles)
    raw = {a.url: _tokenize_signature(f"{a.title}\n{a.body_clean}") for a in articles}

    df = Counter()
    for tokens in raw.values():
        for tk in tokens:
            df[tk] += 1

    max_df = max(SIGNATURE_MIN_DF_CAP, int(total * SIGNATURE_MAX_DF))
    return {
        url: {tk for tk in tokens if df[tk] <= max_df}
        for url, tokens in raw.items()
    }


def jaccard(a: set[str] | list[str], b: set[str] | list[str]) -> float:
    """두 키워드 집합의 Jaccard 유사도 (교집합 / 합집합). 0.0 ~ 1.0"""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def same_event(
    a: set[str],
    b: set[str],
    max_similarity: float = MAX_SIMILARITY,
) -> bool:
    """두 시그니처가 같은 사건인가. 사건 묶기·레인 간 제외·사건 이력이 모두
    이 함수 하나로 판정한다 — 곳마다 기준이 다르면 한쪽에서 묶인 쌍이
    다른 쪽에서 새어 나간다.

    Jaccard ≥ max_similarity 이거나, 겹침 계수 ≥ EVENT_OVERLAP 이면서 공유
    토큰이 EVENT_OVERLAP_MIN_SHARED 이상이면 같은 사건이다.
    """
    if not a or not b:
        return False
    if jaccard(a, b) >= max_similarity:
        return True
    if EVENT_OVERLAP <= 0:
        return False
    shared = len(a & b)
    return (
        shared >= EVENT_OVERLAP_MIN_SHARED
        and shared / min(len(a), len(b)) >= EVENT_OVERLAP
    )


def cluster_articles(
    ranked: list[Article],
    signatures: dict[str, set[str]],
    max_similarity: float = MAX_SIMILARITY,
) -> list[list[Article]]:
    """같은 사건을 다룬 기사들을 묶는다.

    들어온 순서대로 훑으며, 기존 클러스터의 첫 기사와 시그니처 유사도가
    max_similarity 이상이면 그 클러스터에 넣고, 아니면 새 클러스터를 만든다.
    클러스터 순서와 각 클러스터의 첫 원소는 들어온 순서를 따른다.
    rank() 는 검색 순서로 넘기므로 첫 원소가 그 사건의 최상위 검색 기사다.
    대표 기사는 따로 고른다(_pick_representative).
    """
    clusters: list[list[Article]] = []
    for a in ranked:
        sig_a = signatures.get(a.url, set())
        placed = False
        for cluster in clusters:
            if same_event(
                sig_a, signatures.get(cluster[0].url, set()), max_similarity
            ):
                cluster.append(a)
                placed = True
                break
        if not placed:
            clusters.append([a])
    return clusters

def _pick_representative(cluster: list[Article]) -> Article:
    """클러스터의 대표 기사.

    점수 1위가 아니라 '본문이 가장 충실한' 기사를 고른다. 같은 사건이라도
    사진기사(본문 42자)와 심층기사가 섞여 있기 때문이다. 동률이면 점수順.

    다만 제목에 [포토]·[사진] 표지가 붙은 기사는 본문이 길어도 뒤로 민다.
    대표의 본문이 그대로 초안 소스가 되므로, 캡션이 대표가 되면 LLM 이
    쓸 재료가 없어 지어내게 된다. 클러스터가 전부 사진기사면 그중 가장
    긴 것이 대표가 된다 (정렬 키의 첫 항목이 전부 False 가 되므로).
    """
    return max(
        cluster,
        key=lambda x: (
            not is_caption_title(x.title),
            len(x.body_clean),
            x.keyword_score,
        ),
    )

def apply_cluster_bonus(clusters: list[list[Article]]) -> list[Article]:
    """보도 건수만큼 대표 기사에 가점을 주고, 대표만 반환한다.

    가점 = 원점수 × (1 + CLUSTER_BONUS × (보도 매체 수 - 1))  (상한 CLUSTER_BONUS_MAX)
    예) 3개 매체가 보도 → 1 + 0.35×2 = 1.7 → 상한 1.5 적용

    대표 목록은 클러스터 순서(= 검색 순서) 그대로다. 점수로 다시 줄 세우지
    않으며, 가점은 참고 점수(score_norm)에만 반영된다.
    """
    representatives: list[Article] = []
    for cluster in clusters:
        rep = _pick_representative(cluster)
        rep.report_count = len(cluster)
        if len(cluster) > 1:
            factor = min(1 + CLUSTER_BONUS * (len(cluster) - 1), CLUSTER_BONUS_MAX)
            rep.keyword_score = round(rep.keyword_score * factor, 4)
            others = ", ".join(a.press or a.source for a in cluster if a is not rep)
            log.info(
                f"사건 묶음 {len(cluster)}건(×{factor:.2f}): {rep.title[:35]} "
                f"[함께 보도: {others[:60]}]"
            )
        representatives.append(rep)

    return representatives


def article_search_position(a: Article) -> tuple[str, int]:
    """Article 의 (검색 키워드, 검색 순위)."""
    return a.search_keyword, a.search_rank


def order_by_search(
    items: list[T],
    get: Callable[[T], tuple[str, int]] = article_search_position,
) -> list[T]:
    """네이버 검색 순서로 정렬한 새 목록을 돌려준다.

    키워드별로 검색 순위 순으로 줄을 세운 뒤, 줄에서 한 건씩 번갈아 뽑는다.

        국기원 5위 → 태권도협회 1위 → 국기원 9위 → 태권도협회 2위 → ...

    이 함수는 레인 하나의 기사만 받는다(rank() 가 레인별로 불린다). 그래서
    여기서 섞이는 것은 같은 카테고리 안의 키워드들뿐이고, 레인 간 우선순위는
    collect_workflow 의 레인 순서가 그대로 지킨다.

    예전에는 키워드를 섞지 않고 앞 키워드를 통째로 먼저 뒀다. 키워드 하나가
    곧 레인이던 시절의 규칙인데, 한 레인에 키워드가 둘 이상 들어가면서
    앞 키워드가 뒤 키워드를 굶긴다. (2026-09-21: 조직 레인 선정 4건이 전부
    '국기원'이고 '태권도협회'는 0건 — 국기원 1~100위가 태권도협회 1위보다
    앞에 서기 때문이다)

    순위 숫자로 한 줄로 합치지 않는 이유: 검색 순위는 질의마다 따로 매겨진
    값이라 '국기원 5위'와 '태권도협회 1위'를 숫자로 비교할 근거가 없다.
    번갈아 뽑으면 어느 키워드도 굶지 않고, 묶음 4건이 두 키워드에 고르게
    배분된다.

    동률(같은 순번)일 때는 LANES 에 적은 키워드 순서가 앞선다. 목록에 없는
    키워드는 그 뒤에 둔다. 검색 정보가 없는 항목(수동 등록 등)은 맨 뒤에,
    들어온 순서대로 둔다.

    get 으로 위치를 읽는 방법을 바꿀 수 있다 (기본: Article 의 검색 위치).
    """
    kw_order = {k: i for i, k in enumerate(KEYWORD_ORDER)}
    queues: dict[str, list[T]] = {}
    extra: list[T] = []
    for it in items:
        keyword, search_rank = get(it)
        if keyword and search_rank:
            queues.setdefault(keyword, []).append(it)
        else:
            extra.append(it)

    keys = sorted(queues, key=lambda k: kw_order.get(k, len(kw_order)))
    for k in keys:
        queues[k].sort(key=lambda it: get(it)[1])

    merged = [
        it
        for row in zip_longest(*(queues[k] for k in keys))
        for it in row
        if it is not None
    ]
    return merged + extra


def _search_label(a: Article) -> str:
    """로그·알림용 검색 위치 표시. 예) '태권도 3위'"""
    if a.search_keyword and a.search_rank:
        return f"{a.search_keyword} {a.search_rank}위"
    return "검색 외"


def _order_label() -> str:
    """선정 순서 방식을 로그에 한 마디로 남긴다."""
    if CLUSTER_PRIORITY_MIN <= 0:
        return "보도 매체 수 순"
    if CLUSTER_PRIORITY_MIN == 1:
        return "검색 순서"
    return f"{CLUSTER_PRIORITY_MIN}매체 이상 우선"


def rank(
    articles: list[Article],
    top_n: int = TOP_N,
    *,
    return_clusters: bool = False,
    signatures: dict[str, set[str]] | None = None,
):
    """사건을 묶은 뒤 보도 매체 수가 많은 순으로 top_n개를 고른다.

    동률이면 네이버 검색 순서가 정한다. 코드는 주제 부적합 기사와 같은
    사건의 중복 보도만 거른다. 트렌드 점수는 순서에 쓰지 않는다
    (모듈 설명 참고).

    return_clusters=True 면 (선정 목록, 선정분 각각의 사건 기사 목록)을 돌려준다.
    같은 사건으로 합쳐진 기사도 처리 이력에 남겨야, 다음 실행에서 그 기사가
    대표로 다시 뽑혀 같은 사건 글이 두 번 나가지 않는다.

    signatures 를 주면 그것으로 사건을 묶는다. collect 는 세 레인 후보 전체로
    만든 시그니처를 넘긴다 (아래 3차 주석 참고). 주지 않으면 여기 후보만으로
    만든다 (check_ranking 등 단독 호출).

    선정을 소주제 생성보다 먼저 하는 이유:
      - 소주제 생성은 LLM 호출 → 전체 기사에 돌리면 무료 티어를 낭비
      - 사람이 검토할 소주제 수도 함께 줄어 승인 게이트 부담이 작아짐
    """
    if not articles:
        return ([], []) if return_clusters else []

    ordered = order_by_search(articles)

    # 1차: 주제 적합성 — 도메인 키워드가 부족한 기사는 후보에서 제외
    #      (TOP_N을 무조건 채우면 태권도 무관 기사가 섞여 들어온다)
    candidates = []
    for a in ordered:
        ok, reason = is_on_topic(a)
        if ok:
            candidates.append(a)
        else:
            log.info(
                f"주제 부적합({reason}), 후보 제외: [{_search_label(a)}] {a.title[:40]}"
            )

    if not candidates:
        log.warning("도메인 조건을 만족하는 기사가 없습니다")
        return ([], []) if return_clusters else []

    # 2차: 트렌드 점수 — 순서에는 쓰지 않는다.
    #      선정 근거(매칭키워드)와 SEO 대표 키워드, 참고 점수로만 남긴다.
    #      키워드 통계는 전체 기사 기준으로 산출한다.
    weights = extract_trend_keywords(articles)
    for a in candidates:
        a.keyword_score, a.matched_keywords = score_article(a, weights)

    # 3차: 같은 사건 묶기 — 검색 순서로 훑으므로 사건의 자리는 그 사건 기사 중
    #      가장 앞선 검색 순위가 정한다. 대표는 본문이 가장 충실한 기사다
    #      (검색 1위가 사진기사일 수 있다).
    #
    #      시그니처는 일반어를 문서 빈도로 걸러 낸다. 레인 후보만으로 만들면
    #      기준 집합이 작아서 그 레인의 핵심 고유명사가 일반어로 잘려 나간다.
    #      2026-09-22 조직 레인: 같은 사건인 '국제대회 3종 춘천 유치' 두 기사가
    #      따로 선정돼 한 글에 두 챕터로 들어갔다. 세 레인 전체(131건)로 만든
    #      시그니처에서는 J 0.135 로 묶이는 쌍이다. 주제 판정으로 후보가 19건으로
    #      줄면서 공유 토큰(춘천·WT·조정원·총재 등)이 일반어로 잘린 것으로 본다.
    #      레인 간 제외는 원래 전체 시그니처를 쓰므로, 같은 기준으로 맞춘다.
    if signatures is None:
        signatures = build_signatures(candidates)
    clusters = cluster_articles(candidates, signatures)
    # 4차: 보도 매체 수 — 여러 매체가 같은 날 다뤘다는 것이 뉴스 가치다.
    #      CLUSTER_PRIORITY_MIN 이 방식을 고른다 (상수 설명 참고).
    #      어느 방식이든 동률은 검색 순서(= 원래 자리)가 정하므로 인덱스를
    #      보조 키로 쓴다. clusters 와 representatives 는 같은 자리끼리 짝이다.
    #      아래 로그와 return_clusters 가 그 짝을 전제하므로 함께 옮긴다.
    representatives = apply_cluster_bonus(clusters)
    if CLUSTER_PRIORITY_MIN <= 0:
        def _order_key(i: int) -> tuple:
            return (-representatives[i].report_count, i)
    else:
        def _order_key(i: int) -> tuple:
            return (representatives[i].report_count < CLUSTER_PRIORITY_MIN, i)

    order = sorted(range(len(clusters)), key=_order_key)
    clusters = [clusters[i] for i in order]
    representatives = [representatives[i] for i in order]
    selected = representatives[:top_n]

    # 참고 점수: 선정분 중 최고점을 100으로 (순서와 무관하므로 max 로 잡는다)
    top_score = max((a.keyword_score for a in selected), default=0.0)
    for a in selected:
        a.score_norm = round(a.keyword_score / top_score * 100) if top_score else 0

    log.info(
        f"기사 선정 {len(selected)}/{len(articles)}건 "
        f"(주제 적합 {len(candidates)}건 · {len(clusters)}개 사건 중 "
        f"{_order_label()} 앞 {len(selected)}개)"
    )
    for i, (cluster, a) in enumerate(zip(clusters, selected), 1):
        head = cluster[0]
        via = f" (사건 첫 노출: {_search_label(head)})" if head is not a else ""
        cnt = f" · {a.report_count}개 매체" if a.report_count > 1 else ""
        log.info(
            f"  [{i}] {_search_label(a)}{via} · 참고 {a.score_norm:3d}점{cnt} | {a.title[:40]}"
        )
        log.info(f"      기여 키워드: {', '.join(a.matched_keywords[:5])}")
    if return_clusters:
        return selected, clusters[: len(selected)]
    return selected
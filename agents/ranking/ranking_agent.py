"""기사 랭킹 (A 방식: 당일 수집 기사 자체의 빈출 키워드 기반).

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
import re
from collections import Counter

from core.logger import get_logger
from core.article_models import Article

log = get_logger(__name__)

# ── 파라미터 ───────────────────────────────────────────
TOP_N = 5              # 소주제 생성으로 넘길 상위 기사 수 (LLM 호출량 = 이 값)
# --- 주제 적합성 (절대 기준) ---
# 상대 기준(점수 비율·유사도)만으로는 후보가 적은 날 무관 기사가 통과한다.
# 후보가 2건이면 '그 중 1위'가 무조건 100점이 되기 때문이다.
# 실측 지표 (고유 도메인 키워드 수):
#   태권도가 주제인 기사      : 7 ~ 15개
#   태권도가 스쳐 언급된 기사  : 2 ~ 3개  (예: 예산 항목 중 하나, 선수 아버지 직업)
MIN_DOMAIN_HITS = 5        # 제목에 태권도 고유어가 있을 때 필요한 고유 키워드 수
MIN_DOMAIN_HITS_NO_TITLE = 8  # 제목에 없으면 더 엄격하게 (본문만으로 판단해야 하므로)

# 태권도 고유어(CORE_KEYWORDS)가 최소 몇 개 나와야 하는지.
# 이 조건이 없으면 일반 스포츠 용어만으로 통과한다.
# 실측: 태권도 기사 4~13개 / 수영·e스포츠 기사 0~1개
# 실측(2026-08, 선정됨 43건) 분포:
#   정상 태권도 기사   3 ~ 16개  (최저 3: 킴미 기자수첩, 독일 ITF 틀투어)
#   무관 기사          0 ~ 2개   (수영·육상·e스포츠)
# 두 구간이 2와 3 사이에서 갈린다.
MIN_CORE_HITS = 3

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

# 밀도 검사를 적용할 최소 본문 길이.
# 예전에는 2,000자였는데, 그 아래 기사가 검사를 통째로 건너뛰면서
# 1,908자짜리 육상 기사와 1,466자짜리 e스포츠 기사가 통과했다.
# 짧은 정상 기사는 밀도가 오히려 높으므로(494자 기사가 10.12) 낮춰도 안전하다.
DENSITY_CHECK_MIN_CHARS = 600
MIN_SCORE_RATIO = 0.25 # 1위 점수의 이 비율 미만이면 제외 (무관 기사 방지)
# --- 사건 클러스터링 / 보도 건수 가점 ---
# 여러 매체가 같은 사건을 동시에 보도하면 뉴스 가치가 높다는 신호다.
# (네이버가 관련뉴스를 묶어 상위 노출하는 것과 같은 원리)
# 그래서 같은 사건을 '제외'하는 대신, 묶어서 가점을 주고 대표 1건만 선정한다.
CLUSTER_BONUS = 0.35   # 보도 매체가 1곳 늘어날 때마다 점수에 곱해지는 가산 비율
CLUSTER_BONUS_MAX = 1.5  # 가점 상한 (한 사건이 상위를 독식하지 않도록)

MAX_SIMILARITY = 0.15  # 시그니처 유사도가 이 이상이면 같은 사건으로 묶는다.
                       # 실측 분포: 같은 사건 0.26~0.31, 다른 사건 0.00~0.04
                       # → 두 구간 사이 값. 사건 수가 적은 날 후보가 줄어들면
                       #   0.2~0.25로 올려 완화할 수 있다.
SIGNATURE_MAX_DF = 0.5 # 전체 기사의 이 비율을 넘게 등장하는 토큰은 시그니처에서 제외
                       # (태권/대회/선수 같은 일반어는 사건을 구분하지 못함)
SIGNATURE_MIN_DF_CAP = 3  # 위 비율로 계산한 상한의 하한값.
                       # 후보가 적을 때 비율만 쓰면 상한이 1~2가 되어,
                       # '같은 사건이 공유하는 토큰'까지 제외돼 유사도가 0이 된다.
TREND_KEYWORD_N = 15   # 트렌드 키워드 후보 수 (B 방식의 데이터랩 조회 대상이 됨)
MIN_DF = 2             # 최소 2개 기사에 등장해야 트렌드로 인정
DOMAIN_WEIGHT = 2.0    # 태권도 도메인 키워드 가중치
TITLE_BONUS = 1.5      # 제목에 등장하는 키워드 가중치 배수

# ── 태권도 도메인 사전 (2층 구조) ──────────────────────
# 예전에는 한 덩어리였다. 그러다 보니 '대회·선수·감독·금메달' 같은 일반
# 스포츠 용어가 태권도 신호로 계산돼, 태권도가 한 글자도 없는 수영·육상·
# e스포츠 기사가 도메인 키워드 16개로 통과했다.
# (실측: 수영 기사 16개 > 진짜 태권도 기사 13개)
#
# 그래서 '태권도에서만 쓰는 말'과 '어느 종목에나 나오는 말'을 나눈다.
# 통과 판정은 CORE 를 기준으로 하고, GENERIC 은 점수 가중치에만 쓴다.

# 태권도 기사에만 나오는 말. 다른 종목 기사에는 거의 등장하지 않는다.
CORE_KEYWORDS = {
    # 종목·기술
    "태권도", "태권", "겨루기", "품새", "격파", "발차기", "도복", "띠",
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


def _is_core(keyword: str) -> bool:
    """태권도 고유어인지 (주제 적합성 판정용)."""
    return _matches(keyword, CORE_KEYWORDS)


def count_domain_hits(article: Article) -> int:
    """기사에 등장하는 '고유' 도메인(태권도) 키워드 수.

    트렌드 점수만으로는 태권도와 무관한 기사가 일반어 매칭으로 상위에 올 수 있다.
    이 값으로 주제 적합성을 별도 검증한다."""
    tokens = set(tokenize(f"{article.title}\n{article.body_clean}"))
    return sum(1 for tk in tokens if _is_domain(tk))


def count_core_hits(article: Article) -> int:
    """기사에 등장하는 '고유' 태권도 전용 키워드 수.

    일반 스포츠 용어를 뺀 값이라, 종목을 구분하는 실질 신호다.
    """
    tokens = set(tokenize(f"{article.title}\n{article.body_clean}"))
    return sum(1 for tk in tokens if _is_core(tk))


def has_domain_in_title(article: Article) -> bool:
    """제목에 도메인(태권도) 키워드가 있는지.

    실측상 가장 강한 판별 신호다. 태권도가 주제인 기사는 제목에 태권도/국기원/
    겨루기 등이 거의 반드시 등장하고, 스쳐 언급된 기사는 제목에 나오지 않는다.
    (예: '무주군, 국가예산 확보 총력' / '한여름 끓어오르는 KIA 톱타자')

    CORE 기준으로 본다. 일반어까지 인정하면 '나고야 아시안게임' 제목이
    '제목O'로 판정돼 기준이 8개에서 5개로 완화된다. 제목 조건이 오히려
    무관 기사의 통과를 도왔던 원인이다.
    """
    return any(_is_core(tk) for tk in tokenize(article.title))


def is_on_topic(article: Article) -> tuple[bool, str]:
    """태권도가 '주제'인 기사인지 판정. (통과여부, 사유)

    상대 기준과 달리 후보 수에 영향받지 않는 절대 기준이다.
    세 관문을 모두 통과해야 한다.

        1) 고유어 개수   태권도 전용 용어가 MIN_CORE_HITS 개 이상
        2) 고유어 밀도   긴 기사는 1,000자당 고유어가 일정 수준 이상
        3) 전체 개수     기존 도메인 키워드 개수 기준 (제목 유무로 완화)

    1)이 종목을 가르고, 2)가 종합 기사를 거르고, 3)이 얕은 언급을 거른다.
    """
    core = count_core_hits(article)
    if core < MIN_CORE_HITS:
        return False, f"태권도 고유어 {core}개 / 최소 {MIN_CORE_HITS}개"

    in_title = has_domain_in_title(article)

    # 종합 기사 방어: '대학 브리핑 모음', '패트롤' 같은 6,000자짜리 기사는
    # 태권도가 한 단락만 차지해도 고유어 개수는 채운다. 길이로 나눠 본다.
    chars = len(article.body_clean)
    if chars >= DENSITY_CHECK_MIN_CHARS:
        density = core / (chars / 1000)
        min_density = MIN_CORE_DENSITY if in_title else MIN_CORE_DENSITY_NO_TITLE
        if density < min_density:
            where = "제목O" if in_title else "제목X"
            return False, (
                f"고유어 밀도 {density:.2f}/1000자 "
                f"(고유어 {core}개, 본문 {chars:,}자) / 기준 {min_density} ({where})"
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


def cluster_articles(
    ranked: list[Article],
    signatures: dict[str, set[str]],
    max_similarity: float = MAX_SIMILARITY,
) -> list[list[Article]]:
    """같은 사건을 다룬 기사들을 묶는다.

    점수 순으로 훑으며, 기존 클러스터의 대표 기사와 시그니처 유사도가
    max_similarity 이상이면 그 클러스터에 넣고, 아니면 새 클러스터를 만든다.
    각 클러스터의 첫 원소가 그 사건의 대표(= 점수가 가장 높은 기사)다.
    """
    clusters: list[list[Article]] = []
    for a in ranked:
        sig_a = signatures.get(a.url, set())
        placed = False
        for cluster in clusters:
            sim = jaccard(sig_a, signatures.get(cluster[0].url, set()))
            if sim >= max_similarity:
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
    """
    return max(cluster, key=lambda x: (len(x.body_clean), x.keyword_score))


def apply_cluster_bonus(clusters: list[list[Article]]) -> list[Article]:
    """보도 건수만큼 대표 기사에 가점을 주고, 대표만 반환한다.

    가점 = 원점수 × (1 + CLUSTER_BONUS × (보도 매체 수 - 1))  (상한 CLUSTER_BONUS_MAX)
    예) 3개 매체가 보도 → 1 + 0.35×2 = 1.7 → 상한 1.5 적용
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

    representatives.sort(key=lambda x: x.keyword_score, reverse=True)
    return representatives


def rank(articles: list[Article], top_n: int = TOP_N) -> list[Article]:
    """기사에 점수를 매겨 상위 top_n건만 반환한다.

    선정을 소주제 생성보다 먼저 하는 이유:
      - 소주제 생성은 LLM 호출 → 전체 기사에 돌리면 무료 티어를 낭비
      - 사람이 검토할 소주제 수도 함께 줄어 승인 게이트 부담이 작아짐
    """
    if not articles:
        return []

    # 1차: 주제 적합성 — 도메인 키워드가 부족한 기사는 후보에서 제외
    #      (TOP_N을 무조건 채우면 태권도 무관 기사가 섞여 들어온다)
    candidates = []
    for a in articles:
        ok, reason = is_on_topic(a)
        if ok:
            candidates.append(a)
        else:
            log.info(f"주제 부적합({reason}), 후보 제외: {a.title[:40]}")

    if not candidates:
        log.warning("도메인 조건을 만족하는 기사가 없습니다")
        return []

    # 2차: 트렌드 점수 계산 (키워드 통계는 전체 기사 기준으로 산출)
    weights = extract_trend_keywords(articles)
    for a in candidates:
        a.keyword_score, a.matched_keywords = score_article(a, weights)

    ranked = sorted(candidates, key=lambda x: x.keyword_score, reverse=True)

    # 3차: 상대 점수 하한 — 1위 대비 너무 낮은 기사는 트렌드와 무관하므로 제외
    top_score = ranked[0].keyword_score
    threshold = top_score * MIN_SCORE_RATIO
    kept = [a for a in ranked if a.keyword_score >= threshold]
    if len(kept) < len(ranked):
        for a in ranked[len(kept):]:
            log.info(
                f"점수 미달({a.keyword_score:.2f} < {threshold:.2f}), 제외: {a.title[:40]}"
            )

    # 4차: 사건 클러스터링 — 같은 사건을 묶어 보도 건수만큼 가점하고 대표 1건만 남긴다
    #      (중복 보도를 버리는 대신 '뉴스 가치 신호'로 활용한다)
    signatures = build_signatures(kept)
    clusters = cluster_articles(kept, signatures)
    representatives = apply_cluster_bonus(clusters)
    selected = representatives[:top_n]

    # 가점 후 최고점을 100으로 환산 (가점으로 순위가 바뀌므로 재계산)
    top_after = selected[0].keyword_score if selected else 0.0
    for a in selected:
        a.score_norm = round(a.keyword_score / top_after * 100) if top_after else 0

    log.info(
        f"기사 선정 {len(selected)}/{len(articles)}건 "
        f"({len(clusters)}개 사건 중 상위 {len(selected)}개)"
    )
    for i, a in enumerate(selected, 1):
        kws = ", ".join(a.matched_keywords[:5])
        cnt = f" · {a.report_count}개 매체" if a.report_count > 1 else ""
        log.info(f"  [{i}] {a.score_norm:3d}점 (원시 {a.keyword_score:.2f}{cnt}) | {a.title[:40]}")
        log.info(f"      기여 키워드: {kws}")
    return selected

def score_and_cluster(
    articles: list[Article], *, dedupe: bool = True
) -> tuple[list[Article], list[tuple[Article, Article]]]:
    """사람이 직접 고른 기사에 점수를 매기고 같은 사건을 묶는다.

    rank() 를 그대로 쓸 수 없는 이유:
      rank() 는 주제 적합성 필터·점수 하한·TOP_N 절단을 함께 수행한다.
      사람이 이미 고른 기사를 코드가 다시 걸러내면 선택을 무시하는 셈이므로,
      여기서는 '채점'과 '같은 사건 묶기'만 한다.

    점수 해석 주의:
      키워드 가중치(DF)를 이 목록 안에서만 계산한다. 당일 전체 기사로 계산한
      collect 단계의 점수와 분모가 달라 절대값을 비교할 수 없다.
      score_norm(1위=100)은 원래부터 상대 지표이므로 그대로 쓸 수 있다.

    반환: (대표 기사 목록, [(병합된 기사, 흡수한 대표), ...])
    """
    if not articles:
        return [], []

    weights = extract_trend_keywords(articles)
    for a in articles:
        a.keyword_score, a.matched_keywords = score_article(a, weights)

    ranked = sorted(articles, key=lambda x: x.keyword_score, reverse=True)

    if not dedupe:
        for a in ranked:
            a.report_count = 1
        _normalize(ranked)
        return ranked, []

    signatures = build_signatures(ranked)
    clusters = cluster_articles(ranked, signatures)

    merged: list[tuple[Article, Article]] = []
    for cluster in clusters:
        rep = _pick_representative(cluster)
        for a in cluster:
            if a is not rep:
                merged.append((a, rep))

    representatives = apply_cluster_bonus(clusters)
    _normalize(representatives)

    log.info(f"사건 묶음 결과: {len(articles)}건 → 대표 {len(representatives)}건")
    for a, rep in merged:
        log.info(f"  병합: {a.title[:35]} → {rep.title[:35]}")
    return representatives, merged


def _normalize(articles: list[Article]) -> None:
    """최고점을 100으로 환산해 score_norm 을 채운다 (사람이 읽는 지표)."""
    top = articles[0].keyword_score if articles else 0.0
    for a in articles:
        a.score_norm = round(a.keyword_score / top * 100) if top else 0
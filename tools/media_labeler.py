"""8단계 — 표시 및 최종 출력 (Labeling & Output).

AI 생성 사실을 표시하고 웹용으로 저장한다. 검수(7단계)와 분리된 독립 단계다.

가시 표시를 고정 문구로 두는 이유:
  실사풍을 쓰는 이상 특정 경기로 오인될 여지가 남는다. 문구를 매번
  자유롭게 쓰면 표현이 흔들려 표시로서 기능하지 못한다. 템플릿으로 고정한다.

비가시 표시(IPTC digitalSourceType)를 함께 넣는 이유:
  캡션은 크롭·재업로드로 사라진다. 메타데이터는 파일에 남는다.
  둘 다 넣어 두면 규제가 강화되거나 해외 트래픽이 들어와도 소급 대응이 필요 없다.

한국 「인공지능 발전과 신뢰 기반 조성 등에 관한 기본법」 제31조는
생성형 AI 결과물의 표시 의무를 규정한다. 의무 주체는 'AI 사업자'이며,
AI 를 도구로 쓰는 이용자는 현행법상 대상이 아니다. 다만 이 워크플로를
서비스 형태로 타인에게 제공하면 사업자에 해당할 수 있다.
정확한 적용 여부는 시행령·가이드라인과 법률 자문으로 확인해야 한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.logger import get_logger

log = get_logger(__name__)

# 자유 문구가 아니라 템플릿이다. 바꾸지 말고 이 상수만 고칠 것.
AI_DISCLOSURE = "AI로 생성한 이미지이며 특정 경기·인물과 무관합니다."

# IPTC/XMP 표준값. 완전 생성물은 trainedAlgorithmicMedia,
# 실사 편집물은 compositeWithTrainedAlgorithmicMedia 를 쓴다.
DIGITAL_SOURCE_GENERATED = "trainedAlgorithmicMedia"
DIGITAL_SOURCE_EDITED = "compositeWithTrainedAlgorithmicMedia"
# 사람이 카메라로 찍은 사진. 스톡 사진이 여기 해당한다.
DIGITAL_SOURCE_CAPTURE = "digitalCapture"

# 생성물이 아닌 것으로 보는 generation["source"] 값.
# hybrid_image_agent 가 스톡을 배정할 때 넣는 값들이다.
STOCK_SOURCES = {"stock", "stock_fallback"}

WEBP_QUALITY = 82  # 삽화 용도에서 육안 차이가 거의 없는 지점


@dataclass
class LabelResult:
    path: Path
    caption: str
    alt_text: str
    sidecar: Path | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "caption": self.caption,
            "alt": self.alt_text,
            "sidecar": self.sidecar,
        }


def is_generated(generation: dict | None) -> bool:
    """AI 생성물인지 판정한다.

    generation 이 없으면 생성물로 본다. 표시가 빠지는 쪽보다 붙는 쪽이
    안전하기 때문이다. 다만 스톡 사진에 생성물 표시가 붙는 것도 사실과
    다르므로, 호출부가 generation 을 넘기도록 하는 것이 먼저다.
    """
    if not generation:
        return True
    return generation.get("source") not in STOCK_SOURCES


def build_alt_text(
    heading: str,
    keywords: list[str] | None = None,
    *,
    generated: bool = True,
) -> str:
    """대체 텍스트. 1단계 키워드를 재활용한다.

    화면 낭독기 사용자에게 '무엇을 그린 그림인지' 전달하는 것이 목적이다.
    소제목을 그대로 쓰되, 생성물일 때만 그 사실을 덧붙인다. 실사 스톡
    사진에 'AI 생성 이미지'를 붙이면 사실과 다른 안내가 된다.
    """
    base = (heading or "태권도 관련 이미지").strip()
    if keywords:
        base = f"{base} ({', '.join(keywords[:3])})"
    return f"{base} — AI 생성 이미지" if generated else base


def _write_metadata(
    image_path: Path,
    *,
    alt_text: str,
    source_type: str,
    generation: dict | None,
    disclosure: str = "",
    credit: str = "",
) -> Path | None:
    """IPTC 메타데이터를 파일에 심고, 항상 사이드카 JSON 도 남긴다.

    사이드카를 따로 두는 이유: WebP/AVIF 변환이나 블로그 업로드 과정에서
    메타데이터가 통째로 날아가는 경우가 많다. 파일 옆의 JSON 은 남으므로
    나중에 '이 이미지가 어떤 파라미터로 만들어졌는지' 추적할 수 있다.
    """
    record = {
        "digitalSourceType": source_type,
        "description": alt_text,
        "generation": generation or {},
    }
    if disclosure:
        record["disclosure"] = disclosure
    if credit:
        record["credit"] = credit
    sidecar = image_path.with_suffix(image_path.suffix + ".json")
    try:
        sidecar.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning(f"사이드카 저장 실패: {e}")
        sidecar = None

    # 파일 내부 메타데이터는 Pillow 로 넣는다. 실패해도 사이드카가 남으므로
    # 여기서 멈추지 않는다.
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            exif = im.getexif()
            # ImageDescription. 생성물이면 고지 문구, 스톡이면 출처를 넣는다.
            exif[0x010E] = disclosure or (f"출처: {credit}" if credit else alt_text)
            exif[0x0131] = f"taekwonw-agent ({source_type})"  # Software
            if credit:
                exif[0x8298] = credit             # Copyright
            im.save(image_path, exif=exif)
    except Exception as e:
        log.info(f"EXIF 기록 생략 ({type(e).__name__}) — 사이드카로 대체")

    return sidecar


def to_webp(image_path: Path, quality: int = WEBP_QUALITY) -> Path:
    """WebP 로 변환한다. 실패하면 원본 경로를 그대로 돌려준다."""
    try:
        from PIL import Image

        out = image_path.with_suffix(".webp")
        with Image.open(image_path) as im:
            im.save(out, "WEBP", quality=quality, method=6)
        return out
    except Exception as e:
        log.info(f"WebP 변환 생략 ({type(e).__name__}): {image_path.name}")
        return image_path


def label(
    image_path: Path,
    heading: str,
    *,
    keywords: list[str] | None = None,
    generation: dict | None = None,
    credit: str = "",
    edited: bool = False,
    convert_webp: bool = True,
) -> LabelResult:
    """이미지 1장에 표시를 붙이고 웹용으로 저장한다.

    edited=True 는 실사 원본을 인페인팅으로 편집한 경우(6단계 산출물)다.
    완전 생성물과 표준 소스 타입이 다르다.

    credit 은 스톡 사진의 출처다("Pexels", "Wikimedia Commons" 등).
    생성물에는 쓰지 않는다.

    생성물과 스톡을 가르는 이유
    -------------------------
    처음에는 모든 이미지에 AI 고지를 붙였다. 삽화가 전부 생성물이던
    시절의 코드다. 지금은 대부분이 실사 스톡 사진이라, 그대로 두면
    사람이 찍은 사진에 'AI로 생성한 이미지'라고 표시된다. 표시가
    없는 것보다 나쁘다. 사실과 다른 안내이기 때문이다.

    IPTC digitalSourceType 도 마찬가지다. 실사 사진에
    trainedAlgorithmicMedia 를 박으면 메타데이터가 거짓이 된다.
    """
    generated = is_generated(generation)
    alt = build_alt_text(heading, keywords, generated=generated)

    if generated:
        source_type = DIGITAL_SOURCE_EDITED if edited else DIGITAL_SOURCE_GENERATED
        disclosure = AI_DISCLOSURE
        caption = AI_DISCLOSURE
        credit = ""
    else:
        source_type = DIGITAL_SOURCE_CAPTURE
        disclosure = ""
        caption = f"출처: {credit}" if credit else ""

    final = to_webp(image_path) if convert_webp else image_path
    sidecar = _write_metadata(
        final,
        alt_text=alt,
        source_type=source_type,
        generation=generation,
        disclosure=disclosure,
        credit=credit,
    )

    return LabelResult(
        path=final,
        caption=caption,
        alt_text=alt,
        sidecar=sidecar,
    )
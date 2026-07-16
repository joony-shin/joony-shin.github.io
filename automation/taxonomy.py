"""통제 어휘(controlled vocabulary) — 태그/섹터/자산 정규화.

LLM 이 자유 생성한 태그·섹터·자산명을 그대로 taxonomy 로 쓰면 글 1개짜리
아카이브 페이지가 폭증한다(AdSense 'low value content' 판정의 주원인).
여기 정의된 canonical 로만 매핑하고, 매핑되지 않으면 버린다.

- 섹터: 16개 canonical. 변형 표기는 키워드로 흡수.
- 자산: 18개 canonical. track_record.py 의 채점 대상과 정렬.
- 태그: 인물 태그(sources.json) + 글 유형 + 주제 화이트리스트만 허용.
"""

from __future__ import annotations

import re

# ─── 섹터 ─────────────────────────────────────────────────────────────────────
# (키워드 목록, canonical). 위에서부터 첫 매치 — 구체적인 것을 앞에 둔다.
_SECTOR_RULES: list[tuple[list[str], str]] = [
    (["방산", "국방", "무기"], "방산"),
    (["반도체", "칩", "파운드리"], "반도체"),
    (["에너지", "정유", "석유", "원유", "가스", "석탄", "신재생", "태양광", "풍력"], "에너지"),
    (["은행", "보험", "증권", "핀테크", "금융", "자산운용", "카드"], "금융"),
    (["헬스케어", "제약", "바이오", "의료", "병원"], "헬스케어"),
    (["소비재", "유통", "리테일", "소매", "식음료", "화장품", "의류", "명품"], "소비재"),
    (["미디어", "엔터", "콘텐츠", "게임", "방송", "광고"], "미디어"),
    (["자동차", "전기차", "모빌리티"], "자동차"),
    (["항공", "여행", "호텔", "레저", "관광"], "항공·여행"),
    (["해운", "운송", "물류", "철도", "택배"], "해운·운송"),
    (["부동산", "건설", "리츠", "주택"], "부동산·건설"),
    (["유틸리티", "전력", "수도"], "유틸리티"),
    (["통신"], "통신"),
    (["소재", "원자재", "화학", "철강", "광산", "비철"], "소재·원자재"),
    (["농업", "식품", "곡물", "비료"], "농업·식품"),
    (["기술", "테크", "소프트웨어", "인터넷", "클라우드", "ai", "사이버보안", "플랫폼", "빅테크", "it"], "기술"),
]

# ─── 자산 ─────────────────────────────────────────────────────────────────────
# 순서 중요: '금리' 류를 '금' 보다 먼저 검사한다.
_ASSET_RULES: list[tuple[list[str], str]] = [
    (["브렌트"], "브렌트유"),
    (["원유", "유가", "wti", "crude"], "WTI 원유"),
    (["천연가스", "가스"], "천연가스"),
    (["원/달러", "원달러", "원화", "환율", "krw"], "원/달러 환율"),
    (["미국 국채", "미국채", "국채금리", "국채 금리", "10년물", "장기금리", "장단기",
      "채권금리", "미 국채", "treasury"], "미국 국채금리"),
    (["유럽 국채", "유로존 국채", "분트", "bund"], "유럽 국채금리"),
    (["한국 국채", "국고채"], "한국 국채금리"),
    (["금리"], "미국 국채금리"),  # 수식어 없는 '금리'류는 미국 기준금리 문맥이 지배적
    (["달러인덱스", "달러 인덱스", "달러지수", "dxy", "달러"], "달러"),
    (["유로"], "유로화"),
    (["엔화", "엔/달러", "엔"], "엔화"),
    (["코스피", "코스닥", "한국 증시", "한국 주식"], "한국 증시"),
    (["유럽 증시", "유럽 주식", "유럽 은행주", "유로스톡스", "stoxx", "dax"], "유럽 증시"),
    (["s&p", "sp500", "나스닥", "다우", "미국 증시", "미국 주식", "뉴욕 증시", "미 증시"], "미국 증시"),
    (["비트코인", "이더리움", "암호화폐", "크립토", "가상자산", "스테이블"], "암호화폐"),
    (["vix", "변동성지수", "공포지수"], "VIX"),
    (["금값", "금 가격", "골드", "gold"], "금"),
    (["은값", "silver"], "은"),
]
_ASSET_EXACT = {"금": "금", "은": "은"}  # 한 글자는 오매칭 방지 위해 exact 만

# ─── 태그 ─────────────────────────────────────────────────────────────────────
# 변형 → canonical 병합 (공백 제거 후 비교)
TAG_CANON = {
    "도널드트럼프": "트럼프",
    "유럽중앙은행": "ECB",
    "미국연준": "연준",
    "크리스틴라가르드": "라가르드",
    "이자벨슈나벨": "슈나벨",
    "미국경제": "경제",
    "금융시장": "시장",
    "이민정책": "이민",
    "ICE": "이민",
    "중동정세": "중동",
    "호르무즈해협": "중동",
    "이스라엘": "중동",
    "선거제도": "선거",
    "선거자금": "선거",
    "정치자금": "선거",
    "중간선거": "선거",
    "유럽경제": "유로존",
    "디지털유로": "유로존",
    "무역": "관세",
    "핵협상": "이란",
}

# LLM 자유 태그 중 살아남을 수 있는 주제 태그 (이 외는 전부 버림)
TOPIC_TAGS = {
    "이란", "중동", "우크라이나", "중국", "관세", "이민", "선거", "대법원",
    "규제", "감세", "재정정책", "노동", "불평등", "주택정책", "외교",
    "유로존", "유가", "AI", "암호화폐", "캘리포니아", "공화당", "민주당",
    "에너지", "반도체",
}

# 글 유형 태그 (시스템이 붙임)
TYPE_TAGS = {"다이제스트", "속보", "투자관점", "적중리뷰", "track-record", "소개"}

MAX_TAGS_PER_POST = 8


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).strip()


def _match_rules(name: str, rules: list[tuple[list[str], str]]) -> str | None:
    low = str(name).lower().strip()
    for keywords, canonical in rules:
        if any(k in low for k in keywords):
            return canonical
    return None


def normalize_sectors(items: list) -> list[str]:
    out: list[str] = []
    for it in items or []:
        c = _match_rules(it, _SECTOR_RULES)
        if c and c not in out:
            out.append(c)
    return out


def normalize_assets(items: list) -> list[str]:
    out: list[str] = []
    for it in items or []:
        s = str(it).strip()
        c = _ASSET_EXACT.get(s) or _match_rules(s, _ASSET_RULES)
        if c and c not in out:
            out.append(c)
    return out


def normalize_tags(raw_tags: list, allowed_extra: set[str] | None = None) -> list[str]:
    """태그를 canonical 로 병합하고 화이트리스트(주제+유형+allowed_extra)만 남긴다."""
    allowed = TOPIC_TAGS | TYPE_TAGS | (allowed_extra or set())
    allowed_cleaned = {_clean(a): a for a in allowed}
    out: list[str] = []
    for t in raw_tags or []:
        key = _clean(t)
        key = _clean(TAG_CANON.get(key, key))
        canon = allowed_cleaned.get(key) or (TAG_CANON.get(key) if TAG_CANON.get(key) in allowed else None)
        if canon and canon not in out:
            out.append(canon)
    return out[:MAX_TAGS_PER_POST]

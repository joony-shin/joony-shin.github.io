# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""투자 관점 사후검증(track record) 자동 생성.

sns_blog.py 가 만든 글의 `## 투자 관점` 표에는 자산별 예상 방향이 들어 있다.
이 스크립트는 일정 기간(기본 7일)이 지난 예측을 실제 시세(야후 파이낸스 무인증
JSON)와 대조해 적중 여부를 채점하고, 한 편의 "적중 리뷰" 글로 묶어 발행한다.

- 가격 소스: query1.finance.yahoo.com/v8/finance/chart (API 키 불필요)
- LLM 미사용(결정론적 채점) — 비용 0, 재현 가능
- 중복 채점 방지: track_state.json 에 (slug::asset) 기록

신뢰 자산(track record)을 쌓아 "예측"이 아닌 "기록과 검증" 브랜드를 만든다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import normalize_assets  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
POSTS = REPO / "content" / "posts"
STATE_PATH = HERE / "track_state.json"

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"

# 검증 파라미터
HORIZON_DAYS = 7          # 발행 후 며칠 뒤 시세로 채점할지
MATURE_BUFFER_DAYS = 2    # 시세가 확정될 여유 (주말/휴장 대비)
UP_DOWN_THRESHOLD = 1.0   # ±1% 이상이어야 상승/하락으로 인정 (그 안은 중립)
VOL_THRESHOLD = 3.0       # "변동성 확대" 예측은 |변동|>=3% 면 적중
MIN_PREDICTIONS = 3       # 이만큼 모여야 리뷰 글 발행 (너무 잦은 글 방지)
MAX_DATE_GAP_DAYS = 7     # 기준일에서 이만큼 안에 거래일이 없으면 가격 없음 처리

# 자산명(LLM 자유 표기) → 야후 심볼. 긴/구체 키워드를 앞에 두어 오매칭 방지.
ASSET_SYMBOLS: list[tuple[list[str], str, str]] = [
    (["국채금리", "국채 금리", "장기금리", "10년물", "국채 수익률", "채권금리"], "^TNX", "미국 10년 국채금리"),
    (["반도체", "필라델피아", "sox", "soxx"], "^SOX", "필라델피아 반도체지수"),
    (["코스닥", "kosdaq"], "^KQ11", "코스닥"),
    (["코스피", "kospi"], "^KS11", "코스피"),
    (["원달러", "원/달러", "달러원", "원화", "환율"], "KRW=X", "원/달러 환율"),
    (["달러인덱스", "달러 인덱스", "dxy", "달러지수", "달러"], "DX-Y.NYB", "달러인덱스"),
    (["브렌트"], "BZ=F", "브렌트유"),
    (["원유", "유가", "wti", "crude"], "CL=F", "WTI 원유"),
    (["천연가스", "가스"], "NG=F", "천연가스"),
    (["금값", "금 가격", "골드", "gold", "금"], "GC=F", "금"),
    (["은값", "silver", "은"], "SI=F", "은"),
    (["나스닥", "nasdaq"], "^IXIC", "나스닥"),
    (["s&p", "sp500", "스탠더드앤푸어스", "스탠더드 앤"], "^GSPC", "S&P500"),
    (["다우", "dow"], "^DJI", "다우존스"),
    (["비트코인", "bitcoin", "btc"], "BTC-USD", "비트코인"),
    (["이더리움", "ethereum", "eth"], "ETH-USD", "이더리움"),
    (["변동성지수", "vix", "공포지수"], "^VIX", "VIX 변동성지수"),
    (["유로"], "EURUSD=X", "유로/달러"),
    (["엔화", "엔/달러", "엔"], "JPY=X", "달러/엔"),
]

DIRECTIONS = {"상승", "하락", "변동성 확대", "중립"}
_EMOJI = "🔺🔻⚡➖"


# ─── 상태 ─────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"verified": []}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── 자산 매핑 ────────────────────────────────────────────────────────────────
def map_symbol(asset_name: str) -> tuple[str, str] | None:
    low = asset_name.lower()
    for keywords, symbol, canonical in ASSET_SYMBOLS:
        if any(k.lower() in low for k in keywords):
            return symbol, canonical
    return None


# ─── 글 파싱 ──────────────────────────────────────────────────────────────────
def _parse_front_date(text: str) -> dt.datetime | None:
    m = re.search(r"^date:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return None
    try:
        d = dt.datetime.fromisoformat(m.group(1).strip())
        return d if d.tzinfo else d.replace(tzinfo=KST)
    except ValueError:
        return None


def _parse_title(text: str) -> str:
    m = re.search(r'^title:\s*"(.*)"\s*$', text, re.MULTILINE)
    return (m.group(1) if m else "").replace('\\"', '"')


def parse_predictions(md_path: Path) -> list[dict]:
    """`## 투자 관점` 표에서 (자산, 예측 방향) 추출."""
    text = md_path.read_text(encoding="utf-8")
    date = _parse_front_date(text)
    if date is None:
        return []
    # '## 투자 관점' 섹션만 잘라낸다 (다음 ## 또는 --- 전까지)
    sec = re.search(r"##\s*투자 관점\s*\n(.*?)(?:\n## |\n---\n|\Z)", text, re.DOTALL)
    if not sec:
        return []
    body = sec.group(1)
    out: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        asset = cells[0]
        direction = cells[1].lstrip(_EMOJI + " ").strip()
        if asset in ("자산", "") or set(asset) <= set("-:"):
            continue  # 헤더/구분선
        if direction not in DIRECTIONS:
            continue
        mapped = map_symbol(asset)
        if not mapped:
            continue  # 시세 매핑 불가 자산은 채점 제외
        symbol, canonical = mapped
        out.append({
            "slug": md_path.stem,
            "title": _parse_title(text),
            "date": date,
            "asset": asset,
            "canonical": canonical,
            "symbol": symbol,
            "predicted": direction,
        })
    return out


# ─── 시세 ─────────────────────────────────────────────────────────────────────
def fetch_closes(symbol: str, start: dt.datetime, end: dt.datetime) -> dict[dt.date, float]:
    p1 = int(start.timestamp())
    p2 = int(end.timestamp())
    q = urllib.parse.urlencode({"period1": p1, "period2": p2, "interval": "1d"})
    url = f"{YF_CHART}{urllib.parse.quote(symbol)}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = (data.get("chart", {}).get("result") or [None])[0]
    if not res:
        return {}
    ts = res.get("timestamp") or []
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    out: dict[dt.date, float] = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(t, tz=UTC).astimezone(KST).date()
        out[d] = float(c)
    return out


def close_on_or_after(closes: dict[dt.date, float], target: dt.date) -> tuple[dt.date, float] | None:
    for i in range(MAX_DATE_GAP_DAYS + 1):
        d = target + dt.timedelta(days=i)
        if d in closes:
            return d, closes[d]
    return None


# ─── 채점 ─────────────────────────────────────────────────────────────────────
def actual_direction(pct: float) -> str:
    if pct >= UP_DOWN_THRESHOLD:
        return "상승"
    if pct <= -UP_DOWN_THRESHOLD:
        return "하락"
    return "중립"


def grade(predicted: str, pct: float) -> bool:
    if predicted == "변동성 확대":
        return abs(pct) >= VOL_THRESHOLD
    return actual_direction(pct) == predicted


def verify(pred: dict) -> dict | None:
    """예측을 시세로 채점. 시세 부족이면 None."""
    base = pred["date"].astimezone(KST)
    closes = fetch_closes(
        pred["symbol"],
        base - dt.timedelta(days=3),
        base + dt.timedelta(days=HORIZON_DAYS + MAX_DATE_GAP_DAYS + 3),
    )
    if not closes:
        return None
    p0 = close_on_or_after(closes, base.date())
    p1 = close_on_or_after(closes, base.date() + dt.timedelta(days=HORIZON_DAYS))
    if not p0 or not p1:
        return None
    (d0, c0), (d1, c1) = p0, p1
    if c0 == 0:
        return None
    pct = (c1 - c0) / c0 * 100.0
    return {
        **pred,
        "d0": d0, "c0": c0, "d1": d1, "c1": c1,
        "pct": pct,
        "actual": actual_direction(pct),
        "hit": grade(pred["predicted"], pct),
    }


# ─── 글 생성 ──────────────────────────────────────────────────────────────────
_ARROW = {"상승": "🔺", "하락": "🔻", "변동성 확대": "⚡", "중립": "➖"}


def build_review_markdown(results: list[dict], today: dt.date) -> tuple[str, str, str]:
    hits = sum(1 for r in results if r["hit"])
    n = len(results)
    rate = round(hits / n * 100) if n else 0
    title = f"투자 관점 적중 리뷰 ({today.isoformat()}) — {n}건 중 {hits}건 적중"
    desc = f"지난 발행 글의 자산 예측을 {HORIZON_DAYS}일 뒤 실제 시세와 대조한 결과 (적중률 {rate}%)"

    lines = [
        f"지난 글들의 `투자 관점`에서 제시한 자산 방향 예측을 발행 {HORIZON_DAYS}일 뒤 "
        f"실제 종가와 대조했습니다. 이번 회차는 **{n}건 중 {hits}건 적중(적중률 {rate}%)** 입니다.\n",
        "\n> 방향은 ±{:.0f}% 이상 움직였을 때만 상승/하락으로 인정하고, 그 안의 변동은 "
        "중립으로 봅니다. ‘변동성 확대’ 예측은 절대 변동폭이 {:.0f}% 이상이면 적중으로 칩니다.\n".format(
            UP_DOWN_THRESHOLD, VOL_THRESHOLD),
        "\n| 발행글 | 자산 | 예측 | 실제 | 변동 | 판정 |",
        "\n|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: (x["hit"], x["date"])):
        verdict = "✅ 적중" if r["hit"] else "❌ 빗나감"
        pred_disp = f"{_ARROW.get(r['predicted'],'')} {r['predicted']}".strip()
        act_disp = f"{_ARROW.get(r['actual'],'')} {r['actual']}".strip()
        link = f"[{r['title'][:32] or r['slug']}](/posts/{r['slug']}/)"
        asset = f"{r['asset']}"
        lines.append(
            f"\n| {link} | {asset} | {pred_disp} | {act_disp} | {r['pct']:+.1f}% | {verdict} |"
        )
    lines.append("\n")

    # 자산별 적중 요약
    by_asset: dict[str, list[dict]] = {}
    for r in results:
        by_asset.setdefault(r["canonical"], []).append(r)
    lines.append("\n### 자산별 적중\n")
    for asset, rs in sorted(by_asset.items(), key=lambda kv: -len(kv[1])):
        h = sum(1 for x in rs if x["hit"])
        lines.append(f"\n- **{asset}**: {len(rs)}건 중 {h}건 적중")
    lines.append("\n")

    body = "".join(lines)
    return title, desc, body


def write_review(results: list[dict], today: dt.date) -> Path | None:
    POSTS.mkdir(parents=True, exist_ok=True)
    slug = f"{today.isoformat()}-track-record"
    path = POSTS / f"{slug}.md"
    if path.exists():
        print(f"  skip (이미 존재): {path.name}")
        return None
    title, desc, body = build_review_markdown(results, today)

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    # taxonomy 통제 어휘로 정규화 (시세 채점용 canonical 과 표기가 다를 수 있음)
    assets = normalize_assets(sorted({r["canonical"] for r in results}))
    tags = ["투자관점", "적중리뷰", "경제"]
    front = (
        "---\n"
        f'title: "{esc(title)}"\n'
        f"date: {dt.datetime.combine(today, dt.time(18, 0), tzinfo=KST).isoformat()}\n"
        "draft: false\n"
        f"tags: [{', '.join(chr(34)+esc(t)+chr(34) for t in tags)}]\n"
        f"asset: [{', '.join(chr(34)+esc(a)+chr(34) for a in assets)}]\n"
        f'description: "{esc(desc)}"\n'
        "---\n\n"
    )
    footer = (
        "\n\n---\n\n"
        "> ⚠️ **투자 유의 고지**: 본 콘텐츠는 과거 발언 해설의 사후 검증 기록이며, "
        "특정 종목·자산의 매수·매도를 권유하는 투자자문이 아닙니다. 과거의 적중이 "
        "미래 수익을 보장하지 않으며, 투자 판단과 결과의 책임은 투자자 본인에게 있습니다.\n"
    )
    path.write_text(front + body.strip() + footer + "\n", encoding="utf-8")
    print(f"  생성: {path.name}")
    return path


# ─── 엔트리 ───────────────────────────────────────────────────────────────────
def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="투자 관점 사후검증(track record)")
    ap.add_argument("--date", help="기준일 YYYY-MM-DD (KST, 기본=오늘)")
    ap.add_argument("--dry-run", action="store_true", help="글 쓰지 않고 채점 결과만 출력")
    ap.add_argument("--min", type=int, default=MIN_PREDICTIONS, help="발행 최소 건수")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(KST).date()
    state = _load_state()
    verified: set[str] = set(state.get("verified", []))

    # 1) 모든 글에서 예측 수집
    all_preds: list[dict] = []
    for md in sorted(POSTS.glob("*.md")):
        if md.stem.endswith("-track-record"):
            continue
        all_preds.extend(parse_predictions(md))
    print(f"[track] 전체 예측 {len(all_preds)}건 발견")

    # 2) 성숙(발행+horizon+buffer 경과) & 미채점만
    mature_cut = today - dt.timedelta(days=HORIZON_DAYS + MATURE_BUFFER_DAYS)
    todo = [
        p for p in all_preds
        if p["date"].astimezone(KST).date() <= mature_cut
        and f"{p['slug']}::{p['asset']}" not in verified
    ]
    print(f"[track] 채점 대상(성숙·미채점) {len(todo)}건")
    if not todo:
        print("[track] 채점할 예측 없음 — 종료")
        return 0

    # 3) 채점
    results: list[dict] = []
    for p in todo:
        try:
            r = verify(p)
        except Exception as e:
            print(f"  [{p['slug']}::{p['asset']}] 오류: {e}")
            r = None
        if r is None:
            print(f"  [{p['slug']}::{p['asset']}] 시세 부족 — 보류")
            continue
        results.append(r)
        verified.add(f"{p['slug']}::{p['asset']}")
        mark = "✅" if r["hit"] else "❌"
        print(f"  {mark} {r['canonical']:14s} {r['predicted']:6s} 실제 {r['pct']:+.1f}% ({r['slug']})")

    if not results:
        print("[track] 채점 성공 0건 — 종료")
        return 0

    hits = sum(1 for r in results if r["hit"])
    print(f"[track] 채점 {len(results)}건 / 적중 {hits}건 ({round(hits/len(results)*100)}%)")

    if args.dry_run:
        print("[track] --dry-run: 글/상태 저장 생략")
        return 0

    if len(results) < args.min:
        print(f"[track] {len(results)}건 < 최소 {args.min}건 — 이번엔 발행 보류(상태도 미저장)")
        return 0

    write_review(results, today)
    state["verified"] = sorted(verified)[-5000:]
    _save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
